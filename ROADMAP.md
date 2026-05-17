# Roadmap To The State-Value Objective

The roadmap aligns code, data, and gate enforcement with the state-value
transition objective. It is split into five sections:

- **Recently Completed** -- shipped infrastructure that earlier roadmap
  versions still listed as open.
- **Active Priorities** -- ordered by what most needs to happen next, given
  the post-TR20/TR21 reality (Stage-3 v2 + Stage-2 density_alt/hr_factor).
- **Hygiene** -- accumulating debt; not blocking, but worth closing on a
  regular cadence so the loop stays clean.
- **Bidirectional trading -> market-making (long-horizon)** -- the
  multi-phase pivot from Over-only directional bets to a two-sided "smart
  market maker" trying to turn a profit on every game through high-volume
  quoting. Strategic, not a one-week task; phases A-E.
- **Operational guidance** -- standing rules for day-to-day session work.

Last roadmap review: **2026-05-17** (Active #13 fast Wilson-UB
demotion shipped: parallel demote check fires in 5-6 days vs the
14d windowed check; 95% one-sided confidence on Wilson UB <
breakeven; daemon bypasses standard cooldown for `fast_demote`
actions. Phase C v2 safety prerequisite met. Earlier same day:
Active #12 settlement-truth verification shipped: cross-checks
every settled bet against MLB ground truth with 7 result codes +
daily-review block + tiered alerts; motivated by Phase C C2 inventory tracker's stale-settlement
finding. First production run found 0 ROI mismatches but flagged
22 missing MLB JSONs (24.7%, real data-refresh gap). **Same-day
followup** root-caused the gap to a 7-day game-scrape lookback +
active-month-only schedule refresh; bumped lookback to 45d, made
schedule cover prior+active month. Re-verified: missing_mlb_data
22 -> 0, also unmasked 1 stale_filled bet. Earlier same day: Phase
C shadow shipped: two-sided
quote engine [C1] + inventory tracker [C2] + inventory shading [C3]
+ hedge-opportunity logging [C4] all live in shadow mode behind
the new `--quote-engine-mode shadow` CLI flag; no order placement;
live trading behavior unchanged. Earlier same day:
Phase B foundation shipped: offline UNDER candidate universe
synthesis [A5 prereq], calibration_health side split [B1], side
field on promote_events + promote CLI [B2], per-side bet_totals
subtotals [B3], paper-mode validation milestone documented [B4]).
Earlier audit on **2026-05-16**:
Active #5 removed as duplicate of the Recently Completed weekly-
rollup entry; self-improvement gaps added as new Actives #9-#13;
Hygiene section #14-#16 split out; bidirectional/market-maker
section added; Phase A UNDER foundation shipped same day -- under-
side book ingestion verified, separate UNDER calibrator, UNDER
state-value report, UNDER walk-forward + certification all live with
first production data.

## Recently completed

- **Active #13: fast Wilson-UB demotion path** *(2026-05-17)* --
  parallel demote check that fires within 5-6 days vs the existing
  14-day windowed verdict. Motivated by Phase C v2 safety: a bad
  two-sided MM promotion leaks inventory both ways and bleeds
  spread, so the existing ~4-week reaction time is unacceptable.
  Fast check carries 95% one-sided confidence on its own and
  bypasses the daemon's standard cooldown.

  **Math** (`_wilson_upper_bound` + `_fast_wilson_demote_from_post_bets`
  in `promote.py`):
  - For N filled bets post-promotion with W wins:
    `wilson_ub = (p_hat + z²/(2N) + z·sqrt(p_hat·(1-p_hat)/N + z²/(4N²)))
    / (1 + z²/N)`  with z=1.645 (one-sided 95%).
  - Breakeven win rate = mean(entry_ask) across the post-window
    bets. Why: payout per win = 1/ask - 1, so expected ROI at win
    rate p = p/ask - 1; ROI=0 when p=ask.
  - **Fires when `wilson_ub < breakeven`** -- even the most
    generous estimate of true win rate puts the policy below
    breakeven, at 95% one-sided confidence.

  **Verdict taxonomy** (new): `fast_demote`, `hold`,
  `insufficient_post_data` (N < 20), `within_grace_period`
  (promotion < 1 day old), `no_promotion_to_demote`.

  **Daemon integration**: fast verdict checked AHEAD of windowed
  verdict in `evaluate_lever`. When `fast_demote` fires:
  - **Bypasses the 14-day cooldown** -- without this, a fast
    demote against a recent promote would always be blocked
    (cooldown counts the original promote action). The bypass
    is recorded as `cooldown_bypassed: true` in the daemon
    decision row for audit transparency.
  - New decision labels `would_demote_fast` / `demoting_fast`
    distinguish fast from windowed in the retrospective and
    daemon staleness check.
  - Per-lever opt-outs still respected (`--no-auto-demote-<lever>`).

  **CLI integration**: `promote.py status` now shows both
  windowed + fast verdicts side-by-side, with post-window
  diagnostics (n, wins, observed WR, Wilson UB, breakeven) on
  the fast row. `_demote_verdict_gate` accepts either `demote`
  or `fast_demote` to proceed.

  **Daily review**: new `_fast_demote_health` block computes
  all four per-lever fast verdicts at refresh time and surfaces
  any `fast_demote` as a critical alert with the full Wilson
  diagnostic. Notes mirror with prefix `Fast-demote:`.

  **First production run on 2026-05-17**: all four levers
  return `no_promotion_to_demote` (the production audit log
  has no actuated promotions yet -- daemon is in preview mode).
  Zero false alerts. The check is ready for the day Phase C v2
  or any other lever ships a real promotion.

  **Risk story for Phase C v2**: 14d check -> ~$400-600 of
  downside before demoting a bad MM promotion. 5-6d check ->
  ~$150-200. The specific safety improvement that makes Phase
  C v2 actuatable.

  **Files**: `scripts/analysis/promote.py` (Wilson math + 4
  per-lever wrappers + status surface + gate update),
  `scripts/analysis/auto_promote_demote_daemon.py` (fast
  verdict adapters + evaluate_lever priority branch + cooldown
  bypass), `scripts/analysis/build_daily_human_review_report.py`
  (`_fast_demote_health` block + Notes prefix), `daemon_retrospective.py`
  (recognize new fast labels in agreement classification),
  3 new test classes (`WilsonUpperBoundMathTests`,
  `FastWilsonDemoteFromPostBetsTests`,
  `PerLeverFastDemoteWrapperTests`,
  `FastVsWindowedDemoteParityTests`, `FastDemoteHealthTests`).
  951 tests + 41 subtests pass.
- **Schedule + game-scrape drift fix** *(2026-05-17, same-day
  followup to Active #12)* -- root-cause investigation of the 22
  missing MLB JSONs surfaced by settlement-truth verification found
  the schedule scraper was running with a stale 7-day lookback +
  an active-month-only schedule refresh. The April 2026 schedule
  file was last touched 2026-04-04 (when MLB Stats API had only
  published Apr 1-4); the daily refresh's 7-day game-scrape window
  never reached back far enough to catch games added later. Two
  surgical fixes:
  - `RefreshConfig.recent_games_lookback_days` default bumped
    7 -> **45 days**. The scraper is idempotent + skip-existing,
    so cost after one-time backfill is minimal; protects against
    the longest tail of "bet still settling + game JSON late."
  - `scrape_active_schedule` now covers **prior month + active
    month** instead of just the active month, via a new
    `_first_of_prior_month` helper that wraps Jan -> Dec correctly.
    Catches late-added or rescheduled games near month boundaries.

  Backfilled by running `scripts/scraping/scrape_mlb_history.py
  --start-date 2026-04-01 --end-date 2026-05-17`: 363 game JSONs
  downloaded, 269 skipped-existing, 0 failed. Re-ran
  settlement-truth verification: `missing_mlb_data` dropped from
  22 -> 0 (a 24.7%-of-filled-bets gap eliminated), `ok` count
  rose 60 -> 81. The backfill ALSO unmasked 1 real `stale_filled`
  bet (26 days old, filled but no settlement event ever fired)
  that had been hidden behind the missing-data bucket -- a real
  Phase C2-class inventory issue worth tracking.

  Files: `scripts/analysis/run_daily_refresh.py` (lookback default
  + `_first_of_prior_month` helper + schedule scrape window),
  `tests/test_run_daily_refresh.py` (renamed test +
  prior-month-wrap test + lookback-default test). 929 tests +
  41 subtests pass.

- **Active #12: settlement-truth verification** *(2026-05-17)* --
  cross-checks every settled bet against the MLB Stats API ground
  truth. Motivated by the Phase C C2 inventory-tracker finding
  that 69 games carry "open" inventory in `live_orders_ledger.jsonl`
  even though many are actually settled. When Phase C v2 actuates
  two-sided quotes against that inventory, stale-settlement bugs
  become **wrong quotes + wrong inventory limits**, so we need
  ground-truth bounded drift before flipping the live actuation
  switch.

  **Verifier** (`scripts/analysis/verify_settlement_truth.py`)
  classifies each filled bet into one of 7 result codes:
  - `ok` -- engine_won matches MLB-derived expected_won AND
    engine_final_total matches MLB total
  - `resolution_mismatch` -- **critical**: won != expected_won.
    ROI math is corrupted; the bet's profit/loss is wrong.
  - `total_mismatch` -- engine_total != mlb_total but both still
    fall on the same side of the line. ROI math preserved, total
    field wrong (lower severity diagnostic).
  - `stale_filled` -- order_status=filled but won is None AND
    MLB says the game IS final. Settlement event never reached
    the bet record. Phase C C2 inventory tracker treats these
    as "open" forever.
  - `game_not_final_yet` -- bet was settled but MLB says game
    not final. Possible data-ordering bug.
  - `missing_mlb_data` -- bet exists but local MLB game JSON
    not found at the expected path (data refresh gap).
  - `not_yet_settled` -- in-progress game, bet correctly not
    yet settled. Expected state; counted for visibility.

  **Daily review block** (`_settlement_truth_health` in
  `build_daily_human_review_report.py`) reads the artifact and
  fires a tiered alert ladder:
  - Critical: ANY `resolution_mismatch` -- single mismatch
    poisons downstream ROI calculations
  - Moderate: `stale_filled >= 1` with oldest-age suffix
  - Moderate: `missing_mlb_data` share >= 10% of filled bets
  - Moderate: `game_not_final_yet >= 1`
  - Stale-artifact: report > 14d old
  All alerts mirror to top-level Notes with prefix
  `Settlement-truth:`.

  **First production run on 2026-05-17** across 179 bet records
  (89 filled+settled, 90 cancelled/error):
  - **60 OK** (67.4%)
  - **0 resolution_mismatch** -- ROI math is intact (good news!)
  - **0 stale_filled** -- session JSON's `won` field is being
    populated even for the bets the LEDGER shows as stale-open.
    The Phase C C2 stale-inventory problem is a ledger-level
    gap, not a session-level one. Settlement-truth v2 will add
    a ledger-vs-session consistency check to close that gap.
  - **7 game_not_final_yet** -- engine settled bets while local
    MLB JSON snapshot showed `Pre-Game` / `Warmup`. The scraper
    polled and saved the file BEFORE the game ended, then
    didn't re-poll after final. Real diagnostic: the scraper's
    final-state polling needs auditing.
  - **22 missing_mlb_data** (24.7%) -- local game JSONs missing
    for 22 of the 89 filled bets. Above the 10% alert threshold;
    surfaces a real data-refresh gap the operator should
    investigate (likely rescheduled-game dates).

  **Phase C v2 prerequisite met**: with this verifier in place,
  before Phase C ships two-sided live actuation, the operator
  has a daily signal on whether inventory math is trustworthy.
  Any new resolution_mismatch fires immediately; stale_filled
  accumulation is visible. The 22 missing-MLB-data backfill
  is the first concrete TODO before paper-validation ends.

  **Files**: new `scripts/analysis/verify_settlement_truth.py`
  (~430 LOC), new `_settlement_truth_health` block in
  `build_daily_human_review_report.py` (~115 LOC), new
  `settlement_truth_verification` refresh step in
  `run_daily_refresh.py`, 2 new test modules
  (`test_verify_settlement_truth.py` 25 tests,
  `SettlementTruthHealthTests` 10 tests in the daily-review
  test file). 927 tests total + 41 subtests pass.
- **Phase C shadow: two-sided quote engine foundation** *(2026-05-17)* --
  third shipment of the Bidirectional-trading roadmap. **Shadow
  mode only**: the engine computes what it WOULD post (bid + ask +
  hedge) per tick and writes to a per-date ledger; no order is ever
  placed. Live trading remains exactly as before today's session.

  Three layered safety guarantees:
  1. **CLI flag default `off`** -- new `--quote-engine-mode {off,
     shadow}` on `live_engine_cli.py` defaults to `off`. Operators
     who don't pass the flag see zero behavior change.
  2. **No order-placement code path in the new module** --
     `live_quote_engine.py` cannot reach the CLOB SDK even by
     mistake. It returns a `QuoteDecision` dataclass; the caller
     appends to JSONL and returns.
  3. **Inventory tracker reads REAL bets only** -- shadow quotes do
     not mutate inventory state, so the operator can compare "what
     shadow quoted" vs "what really happened" as a clean A/B.

  **C2 inventory_tracker (read-only).** New
  `scripts/trading/inventory_tracker.py` aggregates
  `live_orders_ledger.jsonl` into per-game
  `GameInventoryRow{filled_over/under_shares, open_over/under_shares,
  net_over_shares}`. Settled / cancelled / error events are
  excluded; the latest event per bet wins (collapsing the
  append-only stream into current state). Legacy rows without
  `_event` tag fall back to `order_status` for back-compat.
  Verified on real production ledger: 69 games carry inventory
  (some are stale settlements -- useful diagnostic that the
  ledger doesn't always close out, surfaced for future settlement-
  truth work). 20 new tests.

  **C1 live_quote_engine (shadow).** New
  `scripts/trading/live_quote_engine.py` exposes a pure function
  `compute_quote_decision(ctx) -> QuoteDecision` that takes a
  `QuoteDecisionContext` (FV + book + inventory + config) and
  returns the bid + ask + skip reasons. Quote math:
  `bid = max(over_best_bid+min_offset, FV - half_spread - shade)`,
  `ask = min(over_best_ask-min_offset, FV + half_spread - shade)`.
  Quotes never cross the existing book (clamped to
  `best_ask - min_book_offset` and `best_bid + min_book_offset`).
  Skip reasons (string, no enum lookup): `ok`,
  `missing_fair_value`, `missing_over_book`, `under_pair_unavailable`,
  `max_inventory_long`, `max_inventory_short`,
  `quote_inverted_book`. 20 new tests.

  **C3 inventory shading (inside C1).** Signed shade =
  `(net_inventory / max_inventory) * max_shade`, clamped to
  ±max_shade. Positive when long Over -> shifts both quotes DOWN
  (bid less aggressive; ask invites flattening trades). Symmetric
  when short. Max-inventory bound also independently blocks the
  adding-side quote (`max_inventory_long` skips bid, `max_inventory_short`
  skips ask). Default cfg: `max_inventory_per_game=50`,
  `max_shade=0.05`.

  **C4 hedge opportunity (inside C1).** When net inventory > 1
  share AND opposite-side ask is at-or-below fair+premium, the
  decision carries `hedge_opportunity=True`. Long Over + cheap Under
  -> `hedge_side="buy_under"`. Short Over + cheap Over ->
  `hedge_side="buy_over"`. Always logged with `hedge_reason`
  explaining the trigger (or non-trigger).

  **Shadow ledger writer + builder.** `live_quote_engine`'s
  `append_shadow_decision(path, decision)` is best-effort (a write
  failure logs a warning but never raises -- shadow must be
  fail-open). New `scripts/analysis/build_quote_engine_shadow_report.py`
  reads the trailing 7d of shadow ledger rows and emits sections:
  coverage (rows-per-date), quote_emission_rates
  (both_quoted/bid_only/ask_only/neither + skip-reason histograms),
  spread_summary (would-have spread distribution), inventory_summary,
  shade_summary, hedge_opportunities. Wired as new
  `quote_engine_shadow_report` refresh step. Day-zero path (no
  shadow ledger yet) writes an empty payload + markdown rather
  than crashing.

  **Pipeline integration.** `signal_pipeline.py` calls
  `_maybe_emit_shadow_quote` after FV is computed but before the
  post-FV gates run. This means the shadow ledger gets a row for
  EVERY decision moment that produced an FV, regardless of whether
  the OVER late gates ultimately accepted or skipped. The helper
  is wrapped in `try/except` and logs warnings on failure -- the
  shadow ledger must NEVER block live trading.
  `SignalEngine.__init__` gained a `_quote_engine_mode` attr +
  cached `_inventory_snapshot_cache` (built once per session).

  **What's deferred** (explicit non-goals of this shipment):
  - Live two-sided order placement (after B4 paper-validation)
  - Fill simulation against subsequent ticks (Phase C v2)
  - Continuous quote refresh / orderbook maintenance (Phase D1)
  - Per-game P&L attribution (Phase D2)
  - Tick-level inventory snapshot refresh (Phase D)
  - Hard risk limits / kill switches (Phase D4)

  **Files**: new `scripts/trading/inventory_tracker.py`, new
  `scripts/trading/live_quote_engine.py`, new
  `scripts/analysis/build_quote_engine_shadow_report.py`,
  `scripts/trading/live_engine_cli.py` (`--quote-engine-mode`
  flag), `scripts/trading/signal_engine.py` (engine init for
  mode + config + snapshot cache),
  `scripts/trading/signal_pipeline.py` (`_get_inventory_snapshot`
  helper + `_maybe_emit_shadow_quote` hook + call site after
  FV phase), `scripts/analysis/run_daily_refresh.py` (new
  `quote_engine_shadow_report` step). 3 new test modules:
  `test_inventory_tracker.py`, `test_live_quote_engine.py`,
  `test_quote_engine_shadow_report.py`. 892 tests total + 41
  subtests pass.
- **Phase B foundation: symmetric infrastructure** *(2026-05-17)* --
  second shipment of the Bidirectional-trading roadmap. Builds the
  side-aware schema + structure that UNDER live trading will need
  once Phase C ships, plus the A5 offline UNDER candidate universe
  that downstream Phase B drift alerts consume. Real data on
  2026-05-17 already produces meaningful UNDER artifacts:
  1524 UNDER candidate rows synthesized from 64,893 OVER candidates
  on 2026-05-15 (2.35% rate, gated by `under_pair_available=True`),
  with the UNDER calibrator applying Platt scaling per family
  (different params + different method selection vs OVER).

  Phase B intentionally STOPS short of changing the live trading
  runtime -- the live engine still emits Over-only candidates and
  places Over-only orders. What Phase B adds is the
  *schema + plumbing* so when Phase C wires UNDER trading, no
  downstream refactors are needed.

  **A5: Offline UNDER candidate universe synthesis (prereq).** New
  `scripts/analysis/build_under_candidate_universe.py` reads
  existing `<date>_candidates.jsonl` files and writes
  `<date>_under_candidates.jsonl` siblings. For each OVER candidate
  with `under_pair_available=True` AND `fair_value_raw` set,
  synthesizes an UNDER sibling with:
  - `side: "under"`
  - `decision_ask: under_best_ask`
  - `fair_value_raw: 1 - over_fair_value_raw`
  - `fair_value_calibrated: under_calibrator(1 - over_fair_value_raw)`
    when the UNDER calibrator artifact is loaded; falls back to
    `1 - over_fair_value_calibrated` otherwise (preserving the
    OVER calibration adjustment)
  - `decision: "shadow_under"` (no order ever placed)
  - OVER source fields preserved (`over_source_candidate_id`,
    `over_source_decision`, `over_source_fair_value_calibrated`,
    etc.) for traceability without joins.

  Chose offline-synthesis path over live-engine emission because
  every game-state field already lives on the OVER candidate row;
  same precedent as Phase A3 (state-value) + A4 (walk-forward).
  Defers the live-engine UNDER emission to Phase C where two-sided
  quoting requires real-time decisions. New refresh step
  `under_candidate_universe`. 22 new tests in
  `test_build_under_candidate_universe.py`.

  **B1: side-aware calibration_health.** The `calibration_health`
  block now reads the UNDER calibrator artifact alongside the OVER
  one and exposes a parallel `under` sub-block with the same
  metadata shape (artifact_present, artifact_methods_by_family,
  artifact_audit_by_family, method_changes_since_prior, etc.).
  UNDER alerts carry a `under: ` prefix so operators can
  distinguish where each problem originates. OVER alerts stay
  un-prefixed (legacy grep patterns intact).

  Verified on 2026-05-17 production data: OVER methods are
  `{no_score_drift: isotonic, score_event_transition: platt}`;
  UNDER methods are `{no_score_drift: platt, score_event_transition:
  platt}`. Meaningful asymmetry confirms the design rationale --
  the two calibrators ARE solving different problems. No alerts
  fire because both artifacts are healthy. The other six drift
  dimensions (fill_rate, signal_quality, regime_mix, cohort_roi,
  concept_drift, drift_in_drift) defer their splits to Phase C
  when actual UNDER trading enables -- splitting them today would
  produce all-no-data UNDER sides that add noise without value.
  6 new tests.

  **B2: side field on promote_events + promote.py --side flag.**
  `PromotionEvent` dataclass carries a `side` field defaulting to
  "both" (side-symmetric levers: stage2, stage3-v2). `promote.py`
  adds `--side {over,under,both}` to stake-scaling and
  gate-threshold subcommands (default `over`; today's live engine
  is Over-only so any actuation is by definition Over-side until
  Phase C). `latest_promotion_event_for_lever` gained an optional
  `side` filter: `side="over"` matches rows whose side is "over"
  OR "both" (so a Stage-2 promotion that affected both sides
  doesn't get filtered out of an Over-side query). Legacy rows
  without `side` are read as "both" for back-compat.
  11 new tests in `test_promote_side_field.py`; all 46 existing
  promote tests still pass.

  **B3: side field on session bets + daily review per-side
  subtotals.** `_summarize_bets` returns a per-side breakdown in
  `bet_totals.by_side.{over, under}` with parallel structure to
  the top-level aggregate (count, filled, wins, losses, profit,
  stake_or_cost, win_rate, roi). Each compact bet row carries
  `side` for the markdown table. Today UNDER subtotals are
  all-zero (no UNDER bets); Phase C populates them naturally
  without further plumbing. Unknown sides (typos, future "lay"
  values) get their own bucket rather than being silently
  dropped. 6 new tests.

  **B4: paper-mode validation milestone documented.** The
  ROADMAP's Phase B section spells out the 60-session paper-mode
  validation gate UNDER must clear before going live, with
  explicit pass criteria (n_under_outcomes >= 150, UNDER WR within
  5pp of calibrator-predicted WR, UNDER taker ROI > 0%, no
  persistent UNDER drift alerts). Same shape as the OVER walk-
  forward `READY` verdict; UNDER does not skip it.

  **Files**: new
  `scripts/analysis/build_under_candidate_universe.py`,
  `scripts/analysis/promote.py` (PromotionEvent.side, --side
  flag, latest_promotion_event_for_lever side filter, 20
  threaded side=args.side injections in stake-scaling /
  gate-threshold handlers),
  `scripts/analysis/build_daily_human_review_report.py`
  (`_calibration_artifact_metadata` helper, `under` sub-block,
  `_summarize_bets` per-side subtotals),
  `scripts/analysis/run_daily_refresh.py` (new
  `under_candidate_universe` step). 4 new test modules; 843
  total + 41 subtests pass.
- **Phase A foundation: UNDER offline pipeline** *(2026-05-16)* --
  the first concrete shipment of the Bidirectional-trading roadmap.
  All four Phase A pieces (A1-A4) ship together. Pure offline /
  shadow; live trading remains Over-only until Phase B (side-aware
  audit + sessions) and Phase C (two-sided quote engine) land. Real
  data already produces meaningful signal: PRELIMINARY readiness
  (168 outcomes, 26 dates) and a +10.1% UNDER taker ROI on the OVER
  bet population, with the caveat that the population is biased
  toward "trades the OVER bot took and lost" -- Phase B will fix
  this by emitting UNDER candidates the under side would itself
  select.

  **A1: Under-side book ingestion verified + visibility added.**
  Audit found the monitor (`monitor_mlb_polymarket_ou.py:511-513`)
  already polls both Over and Under token books, and `signal_engine`
  already attaches the under-side fields to the book payload before
  the trading pipeline consumes them. The roadmap's original A1
  description (claiming the runtime doesn't capture the under
  book) was based on a stale assumption. Three things shipped:
  - Fixed the stale comment in
    `backtest_ev_policy.py:74-93` so future operators understand
    why the deny prefix is preserved (enforce-mode fail-closed
    safety on a ~50% pair_available rate, NOT "we don't capture
    the under book").
  - New `_under_book_coverage_health` block in
    `build_daily_human_review_report.py` reads
    `model_maturity_report.json` and surfaces
    `under_pair_available_rate` per family + overall, with a
    `Under-book-coverage:` notes-block prefix when below the
    0.50 warn floor. Real data fires the alert (49% production
    coverage).
  - New `test_under_book_ingestion.py`: 5 contract tests that
    `_market_complement_fields` lifts under_* columns correctly,
    and pair-derived columns (`over_under_ask_sum`, `*_no_vig`)
    are None (not 0) when unpaired, so a future refactor cannot
    silently drop the under-side data. 8 new daily-review tests.

  **A2: Separate UNDER calibrator.** `calibrate_signal_probabilities.py
  --side under` flips labels (`under_win = 1 - over_win`) and raw
  probabilities (`p_under = 1 - p_over`), writes a separate
  artifact (`signal_win_calibration_under.json`), and maintains
  separate stability-gate history (`selection_history_under.jsonl`).
  Family split + identity-rejection + stability-gate machinery all
  generalize unchanged. Confirmed on real data: meaningful
  asymmetry vs OVER -- score_event_transition keeps platt but
  with different params (a=0.0087/b=0.77 vs a=0.086/b=-1.02);
  no_score_drift switches to a different method entirely (OVER
  isotonic, UNDER platt). Proves the design rationale: a perfect
  calibrator on Over does NOT trivially flip to Under, so a
  separately-fit curve captures real asymmetry. Wired as a new
  `calibrate_signal_probabilities_under` step in
  `run_daily_refresh.py`. 10 new tests
  (`test_under_calibrator.py`).

  **A3: UNDER state-value transition report.** New
  `scripts/analysis/build_under_state_value_transition_report.py`
  is a sibling to the OVER report with three substantive
  inversions:
  - Outcome flips: `under_hit = not over_hit` (with None
    passthrough for unsettled games).
  - ROI math uses `under_best_ask` (from the candidate row's
    book payload), not `decision_ask` (which is the over ask).
    Rows without `under_pair_available` are excluded from ROI
    aggregation -- they count toward N but their absence from
    the ROI metric is reflected in the `under_ask_coverage`
    field, so the operator can see when the metric is
    representative.
  - Regime classifiers inverted: a NEGATIVE
    `current_state_value_edge` (Over over-valued) is POSITIVE
    for UNDER, not negative. New regimes:
    `low_medium_phantom_negative_current_edge` and
    `high_phantom_positive_current_edge`. No_score_drift
    regime mirrors: `poisson_and_empirical_under_support`
    fires at empirical_edge <= -0.08 AND poisson_edge <= -0.10.
  - Ranked rows surface the MOST negative current-state edge
    first (opposite of OVER report's MOST positive).

  First production run on 2026-05-16 across 4,307 score-event
  candidates: 27.9% under_win_rate but +22.7% under taker ROI
  thanks to the payout asymmetry (mean under_ask 0.285).
  Coverage 43.7% of rows (under_pair_available gap visible).
  Wired as `under_state_value_transition_report` refresh step.
  16 new tests (`test_build_under_state_value_transition_report.py`).

  **A4: UNDER walk-forward + certification.** Two new builders:
  - `under_walk_forward_runner.py` reuses the OVER runner's
    window-planning + training primitives but flips
    `target_win` per row before training and skips the
    `execution_fill` task entirely (no UNDER orders have been
    posted, so no under-side fill history to learn from). Wired
    behind `StalenessCheck` so the heavy retrain skips when its
    input is unchanged.
  - `build_under_walk_forward_certification.py` mirrors the
    Active #1 cert shape -- sample-readiness verdict (READY /
    PRELIMINARY / INSUFFICIENT against UNDER label rows),
    per-cohort scorecard (same dimensions as OVER cert), and
    per-week drift -- but intentionally OMITS the per-gate
    scorecard because no UNDER gates are enforced today, and
    omits fill-rate metrics because there is no UNDER fill
    history.

  Real data on 2026-05-16: 168 UNDER outcomes across 26 dates
  -> PRELIMINARY verdict (between 75/14 and 150/30 thresholds).
  Overall UNDER taker ROI +10.1% on the under-pair-available
  subset; the high signal-win-rate (70.8%) reflects the
  selection bias of the OVER population, not a generalizable
  UNDER edge. The cert correctly notes this in its scope text.
  17 new tests (`test_under_walk_forward.py`); 798 total +
  41 subtests.

  **Files**: `scripts/analysis/backtest_ev_policy.py` (comment
  fix), `scripts/analysis/build_daily_human_review_report.py`
  (new block), `scripts/analysis/calibrate_signal_probabilities.py`
  (`--side` flag), `scripts/analysis/build_under_state_value_transition_report.py`
  (new), `scripts/analysis/under_walk_forward_runner.py` (new),
  `scripts/analysis/build_under_walk_forward_certification.py`
  (new), `scripts/analysis/run_daily_refresh.py` (4 new steps),
  4 new test modules.
- **Daemon staleness alert** *(2026-05-16)* -- new
  `_daemon_staleness_check` in
  `build_daily_human_review_report.py` surfaces a per-lever alert
  when today's verdict says `promote` or `demote` BUT the audit log
  shows no successful action (`promoted` / `forced` / `demoted`) on
  that lever in the last 60 days. Indicates the daemon isn't acting
  on its own signal: cooldown stuck, file path wrong, opt-out flag
  stuck on, daemon-mode `off` by accident.

  Threshold is 60 days (well past the 14d cooldown so a normal
  cooldown-blocked sequence doesn't false-fire). Records are
  attached to the existing `daemon_readiness_health` block under
  `staleness_records` (per-lever shape with lever / verdict_label /
  last_action_date / days_since_last_action / threshold_days);
  alerts mirror to the top-level Notes via the existing
  `Daemon-readiness:` prefix.

  Distinguishes "no successful action ever" from "last action N
  days ago" in the alert text so operators can tell first-time
  staleness from a stale prior promotion.
  Blocked / dry_run rows correctly don't count as actions.

  Verified on 2026-05-16 real data: no records fire (all real
  verdicts are non-actionable today: stage2/stage3-v2 are
  `insufficient_history`, stake-scaling is `need_more_data`,
  gate-threshold is `hold`). 8 new tests; 742 total.
- **Stage-3 v2 verdict-stability gate** *(2026-05-16)* -- the
  Stage-3 v2 promotion verdict gains a second-layer modal check
  that mirrors the calibration method-stability gate. After the
  primary count-based gate (n_drifting >= 5 of trailing 7) produces
  today's verdict, the gate replays the primary verdict against
  history sliced to each of the prior distinct dates and takes the
  modal. If today's verdict differs from an unambiguous modal AND
  we have >= 5 dates of computable history, today's verdict is
  overridden to the modal.

  The primary gate is the data-signal stability primitive (does the
  underlying drift hold?). The secondary gate prevents the
  5-of-7-boundary flap (one day's `max_abs_delta` crossing 0.015
  swings n_drifting between 4 and 5, flipping the primary verdict
  promote <-> hold).

  Audit fields added to the verdict dict, same shape as the
  calibration stability gate:
  ```
  verdict_stability_gate_enabled: true,
  verdict_stability_window: 7,
  verdict_stability_history: ["hold","hold","hold","hold","promote","promote","promote"],
  verdict_stability_modal: "hold",
  verdict_stability_gate_applied: false,
  pre_override_verdict: "promote"
  ```

  Backward-compatible: `stability_gate_enabled=False` kwarg
  disables the gate (used by callers that want raw primary output);
  on clean histories (all-promote or all-hold) the gate is a
  no-op. 9 new tests; 734 total.
- **Demotion-attribution mirror on cohort_roi alerts** *(2026-05-16)*
  -- symmetric to the existing promotion-attribution. When a
  cohort_roi alert fires AND any demotion event exists in the
  trailing 14d, append `[follows: <lever> demotion Nd ago, ...]`.
  Different verb from "[coincides with]" on purpose: a demote was
  supposed to FIX the cohort, so a continuing alert tells the
  operator the demote either didn't help or the cohort has a
  different root cause. Both suffixes (promotion + demotion) can
  coexist on the same alert.

  Refactored `_recent_promotions` to a generic
  `_recent_events_by_direction` helper; `_recent_demotions` is a
  thin wrapper. `cohort_roi_health` payload now also exposes
  `recent_demotions_count`.

  Verified: 9 new tests; 717 total -> 726 after F (full run
  reported 734 after F + G combined).
- **Input-drift audit flag on calibration artifacts** *(2026-05-16)* --
  every per-family `selection_audit` block now carries
  `input_drift_triggered`, `input_drift_status`, and
  `input_drift_major_features`. Operators can see, while reading the
  calibration audit, whether today's method was selected on
  materially-shifted inputs (the runtime calibrator's training
  distribution no longer matches inference).

  Trigger: >= 2 continuous features (metric=="PSI") at
  verdict=="major" (PSI >= 0.25) in `concept_drift_report.json`.
  Categorical TVD (stadium_id) is intentionally excluded -- stadium
  distribution shifts are a real signal but don't directly imply
  calibration error the way continuous-feature shifts do.

  Schema additions to `selection_audit`:
  ```
  "input_drift_triggered": true,
  "input_drift_status": "ok",     // ok | report_missing | report_unreadable
  "input_drift_major_features": [
    {"feature": "stage2_run_env_delta", "psi": 2.5329},
    {"feature": "team_offense_delta",   "psi": 2.5176},
    {"feature": "base_fair_value",      "psi": 1.8325}
  ],
  "input_drift_threshold": 0.25,
  "input_drift_min_features_to_trigger": 2,
  "input_drift_report_generated_at_utc": "..."
  ```

  When the report is missing or unreadable, status falls back to
  `report_missing` / `report_unreadable` with triggered=false (so
  calibration never fails on a missing concept-drift artifact).

  Triggering emits a WARN log line per family so operators see it
  in the refresh stdout, not just buried in the artifact JSON.

  Verified on 2026-05-16 production data: both calibration families
  (`no_score_drift`, `score_event_transition`) carry
  `input_drift_triggered=true` with the same 3 drifted features
  (stage2_run_env_delta PSI 2.53, team_offense_delta 2.52,
  base_fair_value 1.83). Until those features stabilise, every
  daily calibration is being fit on inputs the model has not seen
  before -- the stability gate operators trust is itself standing
  on shifting ground. 10 new tests; 717 total.
- **Concept-drift attribution on cohort_roi alerts** *(2026-05-16)* --
  every cohort_roi_health alert now carries a candidate-root-cause
  suffix listing the top features whose input distribution shifted
  past the major threshold (PSI >= 0.25 / TVD past its threshold).
  Format mirrors the existing promotion-attribution suffix; both can
  appear on the same alert:

  ```
  edge_bucket=0.15-0.18 cohort ROI -26.5% over trailing 7d (n=15, ...)
    [coincides with: stage2 promotion 5d ago]
    [concept-drift: stage2_run_env_delta PSI 2.53, team_offense_delta PSI 2.52,
                    base_fair_value PSI 1.83]
  ```

  Top 5 features by metric value, sorted desc; if more, suffix
  reads `(+N more)`. We don't claim causation -- the cohort lost
  money AND certain inputs shifted, they MIGHT be related -- but
  the suffix shrinks the operator's investigation surface from
  "all 7 features" to the candidates.

  Implementation: `_concept_drift_health` now computes BEFORE
  `_cohort_roi_health` so its `feature_verdicts` are available;
  new `_major_drift_features` + `_attribute_alert_to_concept_drift`
  helpers; cohort_roi_health payload now also exposes
  `concept_drift_major_features_count` for downstream consumers.

  Confirmed on 2026-05-15 production data: 6 cohort alerts all
  now carry the suffix naming 3 currently-drifting inputs
  (stage2_run_env_delta, team_offense_delta, base_fair_value).
  9 new tests; 707 total.
- **Daemon readiness verdict in daily review** *(2026-05-16)* -- new
  `daemon_readiness_health` block in
  `build_daily_human_review_report.py` reads the retrospective JSON
  built by `daemon_retrospective.py` and synthesises an explicit
  go/no-go signal for the operator:
  - per-lever `readiness_for_act` (ready_for_act /
    needs_more_history / disagreements_present)
  - per-lever counts (match / daemon_only / operator_only /
    daemon_disagreed / both_no_action)
  - overall `overall_ready_for_act` (true iff every time-series
    lever is `ready_for_act`)
  - snapshots of the two non-time-series levers carried forward

  Three alert conditions, all mirrored into the top-level Notes
  block with prefix `Daemon-readiness:` (same pattern as the seven
  drift dimensions):
  - **Disagreement**: any lever shows `disagreements_present`
    -> alert names the lever and the most recent
    disagreement date.
  - **Stale**: retrospective JSON > 14d old.
  - **Positive signal**: every time-series lever is
    `ready_for_act` -> "operator may consider
    `--auto-daemon-mode act` after reviewing the per-date table."

  Today (2026-05-16) both levers show `needs_more_history`
  (production history has only 1 distinct date so far); the
  positive signal will fire after 7+ refreshes accumulate clean
  agreement. 7 new tests on the block + Notes mirroring; 698 total.
- **Daemon retrospective backtest** *(2026-05-16)* -- new
  `scripts/analysis/daemon_retrospective.py` replays the auto-daemon's
  promote-decision logic against every historical date in
  `stage2_brier_history.jsonl` and `stage3_v2_drift_history.jsonl`,
  then compares what it WOULD have done vs what the audit log
  (`promote_events.jsonl`) shows the operator actually did. This is
  the evidence path operators use before flipping
  `--auto-daemon-mode preview -> act`.

  Per-(date, lever) decisions are classified into one of five
  agreement buckets:
  - `MATCH` -- daemon and operator both acted, same direction
  - `DAEMON_ONLY` -- daemon would have acted, operator did not
  - `OPERATOR_ONLY` -- operator acted, daemon would not have
  - `DAEMON_DISAGREED` -- daemon would have promoted but operator
    demoted (or vice versa) -- highest severity
  - `BOTH_NO_ACTION` -- both correctly did nothing

  Per-lever readiness verdict synthesises the counts:
  - `ready_for_act` -- ≥ 7 dates evaluated AND zero
    `DAEMON_DISAGREED` AND zero `DAEMON_ONLY`
  - `needs_more_history` -- < 7 dates evaluated
  - `disagreements_present` -- any disagreement or daemon-only

  Cooldown lookup in the replay uses strict-less-than on the date
  (daemon evaluates at morning refresh, so a same-day operator
  action shouldn't make the replay falsely look like a cooldown
  skip).

  Scope of v1: promote-replay only for the two time-series levers
  (stage2, stage3-v2). Snapshot-only for stake-scaling (no
  per-date history file exists) and gate-threshold. Demote-replay
  is deferred -- the demote verdict needs pre/post session windows
  that only ratify after the promotion event's post-window matures
  (~14 days), and at our current sample size (one promotion event
  total) the dimension is trivial.

  Wired as a new `daemon_retrospective` refresh step right after
  `auto_promote_demote_daemon`; outputs land at
  `data/analysis_output/daemon_retrospective/daemon_retrospective.{json,md}`.
  Cheap to refresh -- pure history-file reads + math. First
  production run on 2026-05-16: stage2 + stage3-v2 both
  `needs_more_history` (only 1 distinct date per history file).
  Becomes meaningful once 7+ refresh days have accumulated. 33 new
  tests; 691 total.
- **Runtime-overrides config layer** *(2026-05-16)* -- new
  `cache/live_engine_overrides.json` is the actuation mechanism that
  closes the auto-daemon's biggest gap. Before today, `promote.py
  stake-scaling` and `promote.py gate-threshold` could only PRINT a
  recommended CLI flag and rely on the operator typing it into their
  saved command line; the daemon therefore had to skip those two of
  four levers with `skipped_cli_flag_lever`.

  New pieces:
  - **`scripts/trading/live_engine_overrides.py`** -- pure helpers
    (`load_overrides`, `apply_overrides`, `set_override`,
    `remove_override`, `restore_overrides_from_backup`) and a sparse
    JSON schema covering `calibrated_stake_scale_mode` + the five
    gate thresholds the gate-threshold lever knows about.
  - **`live_engine_cli.py`** -- reads the overrides file at end of
    `parse_live_args` and applies values. Precedence is explicit CLI
    flag > override file > argparse default; we detect "operator
    passed flag" by scanning argv for the long-flag tokens, so the
    operator always wins.
  - **`promote.py stake-scaling` / `gate-threshold`** -- both
    subcommands now mutate the overrides file (with the same
    backup-on-write pattern Stage-2/Stage-3-v2 use); demote variants
    restore from the backup, or remove the override key when no backup
    exists. Gate-threshold values are coerced to the live-engine
    target type (float for most, int for `min_inning`) so argparse-
    typed targets receive the right type.
  - **`auto_promote_demote_daemon.py`** -- stake-scaling now flows
    through the same actuation path as the file-swap levers
    (auto-actuated when verdict says `promote`, with the standard
    14-day cooldown). Gate-threshold remains preview-only: even with
    the override-file mechanism available, the *threshold value* to
    pick from walk-forward certification is a per-gate judgment that
    benefits from operator review.

  Three of four levers (`stage2`, `stage3-v2`, `stake-scaling`) are
  now fully auto-actuated; one (`gate-threshold`) is preview-only by
  design. The system can run unattended for all binary go/no-go
  decisions. 17 new tests in `test_live_engine_overrides.py` +
  updated daemon and promote-CLI tests; 658 total.
- **Drift-in-drift analyzer** *(2026-05-16)* -- the 7th drift dimension
  and 2nd leading indicator. The existing `concept_drift_health` fires
  on day-vs-baseline PSI (>= 0.25). It misses the slow-creep failure
  mode: a feature that drifts ~0.005/day over weeks, never crossing
  the daily threshold but accumulating past it. New
  `scripts/analysis/build_drift_in_drift_report.py` reads
  `psi_history.jsonl` (the append-only per-feature history we shipped
  on 2026-05-15), fits an OLS slope on the trailing 30d of (day_index,
  psi_value) pairs per feature, and projects 30d forward:

  ```
  projected_psi = intercept + slope * (last_day_index + horizon_days)
  ```

  If `projected_psi >= 0.25` -> major alert; `>= 0.10` -> minor note.
  Negative projections clamp to 0 (PSI is non-negative). Insufficient
  history (< 7 distinct points) -> `insufficient_history` verdict, no
  alert. The intercept-based projection avoids double-counting noise
  on the most recent observation.

  Wired as the `drift_in_drift_report` refresh step right after
  `concept_drift_report` (its data source); pipeline now 49 steps.
  Staleness-checked against psi_history mtime. New
  `drift_in_drift_health` block in the daily review mirrors the
  artifact's alerts into the top-level Notes (prefix
  `Drift-in-drift:`).

  Becomes meaningful in ~6 weeks when ~6 weeks of psi history has
  accumulated. Building it now (with synthetic-data tests against the
  math + the analyzer correctly returning `insufficient_history` for
  the empty production state) gets the instrumentation ready for when
  the data is meaningful. 30 new tests; 623 total.
- **EV-policy feature-exclusion fix** *(2026-05-16)* -- closes a
  silent correctness issue caught in the 2026-05-15 audit. The 5/15
  retrain of `ev_signal_win_if_filled_model.json` selected 295
  features including book-pair fields (`under_best_bid`,
  `over_best_ask`, `decision_mid`, etc.) the runtime doesn't supply
  at decision time; live engine logged a missing-features WARNING at
  20:15 (`shadow will score with artifact imputations, enforce will
  fail closed`). Impact in shadow mode: zero. Impact if promoted to
  enforce (Active #4): every bet would fail-closed -- a silent
  blocker that didn't exist before yesterday's retrain.

  Fix in `backtest_ev_policy.py`: extend `RUNTIME_FEATURE_DENY_PREFIXES`
  from `("sim_",)` to `("sim_", "over_", "under_")` and add
  `decision_mid` to the exact-name deny list. Every one of the 20
  `over_*`/`under_*` fields in the unified signals schema is a
  book-pair derivation that requires both sides of the book at
  decision time -- the runtime doesn't capture the under-side book,
  so the entire family is correctly unsafe. The artifact now also
  emits `runtime_feature_exclusion_prefixes` alongside the exact list
  so operators reading the JSON can see BOTH dimensions of exclusion.

  Verified end-to-end: rebuilt EV-policy artifact dropped from 295 to
  275 features; all 21 problem fields excluded; next engine restart
  will load the cleaned artifact and the missing-features warning
  will not re-fire. 5 new tests + 41 subtests; 593 total.
- **Auto promote/demote daemon (preview-default)** *(2026-05-16)* --
  new `scripts/analysis/auto_promote_demote_daemon.py` reads
  today's stability-gate verdicts + the promote_events log; for the
  file-swap levers (`stage2`, `stage3-v2`) invokes `promote.py` when
  a verdict says go AND a 14-day cooldown has elapsed since the last
  action (manual or daemon). Wired as a new refresh step
  (`auto_promote_demote_daemon`) between `weekly_drift_rollup` and
  `artifact_lineage_freshness`; refresh pipeline now 48 steps.

  **Safety contract**:
  - Ships `--auto-daemon-mode preview` by DEFAULT (logs decisions,
    takes no action). Operator opts into `--auto-daemon-mode act` via
    `run_daily_refresh.py --auto-daemon-mode act` only after
    reviewing preview output for several sessions. `--auto-daemon-mode
    off` skips the step entirely.
  - 14-day cooldown lock-in prevents any auto-action within 14 days
    of any prior action on the same lever (manual or daemon), matching
    the demotion-verdict pre/post window so demote signals can gather
    evidence.
  - File-swap levers only (`stage2`, `stage3-v2`). CLI-flag levers
    (`stake-scaling`, `gate-threshold`) get notes in daemon output but
    no auto-action -- they need a runtime-overrides config layer the
    system doesn't have yet; operator runs the promote.py subcommand
    manually for those.
  - Trusts the existing verdicts (no second-layer N-consecutive-
    refresh check). The 5/7-day Brier-stability gate is the stability
    primitive; the daemon's only added protection is the cooldown.
  - Per-lever opt-outs (`--no-auto-promote-stage2` etc.) on the daemon
    CLI for finer-grained control.

  **Audit symmetry**: daemon actions go through `promote.py`, which
  writes to `promote_events.jsonl` with `operator="auto_daemon"`. The
  audit trail distinguishes manual from automated actions without
  needing a separate log. Skip-decisions (cooldown, no-go verdict,
  opt-out) are logged to the daemon's stdout (which lands in the
  refresh manifest's output_tail and the `refresh_health_rollup` INFO
  block) but NOT to promote_events.jsonl -- that log stays focused on
  actual state changes.

  This is the transition from "self-improving with human approver" to
  "self-improving with human reviewer." Every piece of the
  measure→recommend→act→audit→regression-detect→revert loop now exists
  and can run unattended; the operator's role becomes reviewing what
  the daemon did rather than executing each lever themselves. 24 new
  tests; 588 total.
- **Symmetric demotion infrastructure** *(2026-05-16)* -- the
  promotion CLI now has a full inverse: `promote.py demote
  {stage2,stage3-v2,stake-scaling,gate-threshold}`. Closes the
  asymmetry where the system had detection + actuation for promotion
  but only detection (via lagging drift alerts) for regression.

  Four pieces ship together:

  1. **Backup-on-promote**: when `promote.py stage2` (or `stage3-v2`)
     swaps files, the prior production is atomically copied to
     `<file>.prior_promote.json` first. Demotion restores from there.
     The promotion event records `backup_path` so demote knows
     exactly where to look.

  2. **Outcome-based demotion verdicts**: for each lever, compute
     `pre_window` and `post_window` filled-bet ROI from session JSONs
     in the 14 days before/after the most recent promotion timestamp.
     If post is worse than pre by >=10pp AND both windows have >=10
     filled bets -> `demote` verdict. Otherwise `hold` /
     `insufficient_pre_data` / `insufficient_post_data` /
     `no_promotion_to_demote`. Stake-scaling specifically filters to
     bets where `calibrated_stake_multiplier != 1.0` (the bets the
     promotion actually affected); paper-fallback bets are excluded
     everywhere (no real P&L). Demotion-verdict checks are
     intentionally slow -- 14d windows give statistical power at our
     ~3.4-fills/day rate, at the cost of running a bad model for up
     to 4 weeks before demoting.

  3. **`demote` subcommands**: mirror the promote subcommands.
     Stage-2/Stage-3-v2 actually restore files (or delete to fall
     back to compiled defaults when no backup exists from a
     first-promotion case). Stake-scaling/gate-threshold print the
     recommended CLI flag revert. Same refusal-by-verdict +
     `--force` semantics + audit-log row as promotion, with
     `direction: "demote"` and `action: "demoted" | "forced" |
     "dry_run" | "blocked"`.

  4. **Promotion-attribution on drift alerts**: when
     `cohort_roi_health` fires an alert and any promotion event
     exists in the trailing 14 days, the alert text gets a
     `[coincides with: stage2 promotion 5d ago, ...]` suffix. Lets
     the operator see the temporal coincidence between cohort drift
     and a recent promotion without grepping the audit log -- the
     fast signal that complements the 14-day post-hoc demotion
     verdict.

  `promote.py status` now shows both promotion and demotion verdicts
  in one summary. The audit log carries a new `direction` field
  ("promote" | "demote") on every event row; legacy rows without it
  are treated as `promote` for backward-compat reads. 31 new tests;
  564 total.
- **Concept-drift detection on model inputs** *(2026-05-15)* -- new
  `scripts/analysis/build_concept_drift_report.py` adds the first
  *leading* indicator to the drift-alert family. The other five
  dimensions (calibration / fill / signal / regime-mix / cohort-ROI)
  all fire after the model has already started being wrong on real
  money; this one fires when the *inputs the model consumes* shift,
  before the calibration error / cohort losses materialize.

  Method: Population Stability Index (PSI) on continuous features
  (`weather_temp_f`, `weather_wind_out_component_mph`,
  `weather_air_density_index`, `stage2_run_env_delta`,
  `team_offense_delta`, `base_fair_value`) + Total Variation Distance
  on categorical (`stadium_id`), comparing a trailing 7-day window
  against the prior 30 days. PSI verdict cutoffs are textbook
  (`<0.10` stable, `0.10-0.25` minor note, `>=0.25` major alert).
  Equal-frequency 10-bin histograms on the baseline; current
  observations bucketed into those edges with smoothing constant so
  empty current bins don't blow up `log(0)`. Sample-size guard at 30
  rows per feature per window -- below that, verdict is
  `insufficient_data` (no false alerts).

  Wired as a refresh step after `unified_signals` (data source);
  staleness-checked. Output:
  `data/analysis_output/concept_drift/concept_drift_report.{json,md}`
  + append-only `psi_history.jsonl` (one row per feature per day, lets
  future analysis detect *drift in the drift* -- features gradually
  shifting for weeks even if no single day crossed the alert
  threshold). Daily review now carries a `concept_drift_health` block
  that mirrors major alerts into the top-level Notes ("Concept-drift:
  ..."). First production run on 2026-05-14 fired three alerts:
  `stage2_run_env_delta` PSI 3.22, `team_offense_delta` PSI 2.36,
  `base_fair_value` PSI 2.88 -- the inputs the live model is consuming
  this week look meaningfully different from last month, partly
  explained by the post-TR20 trade-volume increase shifting the
  selection bias on which signals reach the model. 24 new tests + 4
  on the daily-review block; 533 total.
- **Unified promotion CLI** *(2026-05-15)* -- new
  `scripts/analysis/promote.py` wraps the four manual self-improvement
  levers (Stage-2 cache swap, Stage-3 v2 weights swap, stake-scaling
  shadow→enforce, gate-threshold RETUNE) behind one command pattern:

  ```
  python scripts/analysis/promote.py status              # all 4 verdicts
  python scripts/analysis/promote.py stage2              [--dry-run] [--force]
  python scripts/analysis/promote.py stage3-v2           [--dry-run] [--force]
  python scripts/analysis/promote.py stake-scaling       [--dry-run] [--force]
  python scripts/analysis/promote.py gate-threshold <name> <value> [--dry-run]
  ```

  Each subcommand reads the relevant verdict file (built daily by
  `run_daily_refresh.py`'s stability gates), refuses to promote unless
  the verdict says go (or `--force` with a logged warning), performs
  the change, and appends a row to a new
  `data/analysis_output/promotion_events.jsonl` audit log. Stage-2 and
  Stage-3 v2 actually swap files atomically; stake-scaling and
  gate-threshold print the recommended `live_engine.py` CLI flag change
  (the operator's saved/memorized command stays the single source of
  truth for runtime config). Closes the gap between "system says
  PROMOTION READY" and "system actually promotes" without removing
  human consent. The promotion events log becomes the audit trail for
  future drift detection ("what changed and when") and for a future
  auto-promotion daemon ("did we already do this today"). 24 new
  tests; 505 total.
- **Cohort-ROI drift alerts** *(2026-05-15)* --
  `build_daily_human_review_report.py` now carries a `cohort_roi_health`
  block alongside the existing four drift-alert dimensions
  (calibration / fill / signal / regime-mix). Companion to
  `regime_mix_health`: that fires on pre-trade *distribution* shifts
  (TVD on which buckets the bot is picking); this fires on *outcome*
  shifts -- a cohort that was profitable but is now losing real money.
  Two flavours of alert: (a) **absolute-losing** (cohort has at least
  5 filled bets in the trailing 7d AND its ROI is below -10%);
  (b) **regime-change** (cohort's trailing-7d ROI is at least 15pp
  worse than its trailing-30d baseline, both meeting the 5-bet floor).
  Five cohort dimensions: edge bucket (0.15-0.18 / 0.18-0.22 / etc.),
  ask bucket, inning, line, current-state-edge bucket. First production
  run on 2026-05-14 fired four alerts that match yesterday's audit
  findings: edge 0.15-0.18 = -22.8% ROI (n=11), inning 6 = -34.1% ROI
  (n=10), current-state-edge >=0.08 = -23.8% ROI (n=12), line >=10.5
  flipped from +9.2% to -9.4% (regime change). Closes the gap where
  cohort-level signals only surfaced during ad-hoc audits, not daily
  review. 11 new tests; 481 total.
- **Errored-bet placement_error preservation + edge alias on
  unified_signals** *(2026-05-15, observability fixes)* --
  `LiveBetRecord` now carries `placement_error` (a truncated copy of
  the raw SDK error message) populated at both the pure error branch
  and the wallet-balance paper-fallback path in
  `live_engine_placement.py`. Closes the audit gap where 62 errored
  placements on 2026-05-12 had only `order_status="error"` with no
  reason recorded structurally. Now any future failure mode (rate
  limit, 401, token mismatch, etc.) is visible in session JSON without
  log-grep. Separately, `unified_signals` rows now carry an `edge`
  alias next to `edge_at_ask` (same value) so ad-hoc consumers reaching
  for `row.get("edge")` don't silently get None/0. Walk-forward
  certification was already correct. 4 new tests; 470 total.
- **Calibration method-stability gate** *(2026-05-14)* -- the daily
  refresh's calibration method selection was platt<->isotonic
  flip-flopping on small validation samples (2026-05-11 → 2026-05-12 →
  2026-05-13: both families flipped, then flipped back). With the
  validation set small enough for logloss noise to dominate the
  ranking, the runtime calibrator was being swapped daily, making the
  calibrator itself a source of FV instability.
  - **Fix**: new `_apply_stability_gate` in
    `calibrate_signal_probabilities.py`. After validation-logloss +
    identity-rejection guard pick today's method, the gate reads the
    trailing-7-day pre-override selections from a new
    `data/analysis_output/calibration/selection_history.jsonl` (one
    row per refresh, per family). If today's pick differs from the
    unambiguous modal of the last 7 distinct dates AND we have ≥ 5
    days of history, override to the modal. Ties in the modal mean
    "no override" (don't lock in an arbitrary tie-break). The gate
    only flips methods after the new method has won enough consecutive
    days to shift the modal, suppressing daily noise without
    preventing genuine drift.
  - **Validated against the real flip**: a smoke test built the
    pre-5/12 stable history (8 days isotonic/platt) and showed both
    5/12 flips would have been caught and overridden to the modal.
  - **Backfill shipped**: prior 10 days of selections backfilled into
    `selection_history.jsonl` from `daily_human_review` snapshots so
    the gate has data from day one. Future refreshes append automatically.
  - **CLI knobs**: `--stability-window 7`, `--stability-min-history 5`,
    `--no-stability-gate`, `--selection-history-path`. Existing tests
    that called `main()` end-to-end were updated to pass
    `--no-stability-gate` so the test suite stops polluting the
    canonical history file.
  - **Audit transparency**: every calibration artifact now records
    `selection_audit.stability_gate_enabled`,
    `stability_modal`, `stability_history`,
    `stability_gate_applied`, and `pre_override_selected` so future
    runs can answer "did the gate fire? what method did today's data
    really want?".
  - 21 new tests; 36 total test modules.
- **Walk-forward certification report (Active #1 prep)** *(2026-05-13)* --
  new `scripts/analysis/build_walk_forward_certification.py` consumes
  `signal_training_table.jsonl` and emits the per-cohort + per-gate
  scorecard Active #1 calls for. Three output sections:
  (a) **Sample-readiness verdict** -- `READY` / `PRELIMINARY` /
  `INSUFFICIENT` based on filled-bet count and date span (thresholds
  150 / 30 for ready; 75 / 14 for preliminary). Verdicts auto-degrade
  with confidence labels so a small sample can never trigger an
  unsafe action. (b) **Cohort breakdowns** across edge band, ask
  band, inning band, runs-needed band, current-state-edge band,
  phantom-risk band, and signal family -- each with N, fill-rate,
  filled-WR, signal-WR, P&L, ROI, max drawdown. (c) **Per-gate
  scorecard** for each enforced gate (`gate_extreme_edge`,
  `gate_min_edge`, `gate_min_inning`, `gate_min_entry_ask`,
  `gate_runs_needed_max`): sweep alternative thresholds and emit a
  `KEEP` / `RETUNE` / `RETIRE` verdict with confidence based on
  filtered-vs-kept cohort sample sizes. Wired as a refresh step so
  the artifact stays fresh; on Active #1's day-30 trigger we just
  re-read the same paths instead of designing the report under
  deadline. **Preliminary findings on the 53 filled bets we have
  today** (INSUFFICIENT label, directional only): edge >0.22 cohort
  is -69% ROI vs +11% ROI for the kept cohort -- gate_extreme_edge
  is doing real work. ask-band <0.65 is -47% ROI; current-state-edge
  band <0.03 is +8.5% ROI vs >=0.08 -20% ROI (counterintuitive --
  worth tracking). 18 new tests; 35 total.
- **Wallet-aware paper fallback** *(2026-05-13)* -- when the CLOB
  rejects a real-money order with `not enough balance / allowance`,
  the live engine no longer drops the bet (which lost data) or retries
  the same signal up to 10× (yesterday's session burned 62 attempts on
  the same handful of signals). Instead the bet is routed to a
  **synthesized paper-fallback**: filled at the limit price, tracked
  through the normal settlement path, marked
  `placement_mode="paper_fallback"` and
  `paper_fallback_reason="clob_balance_error"` for downstream
  filtering. A session-level cooldown (default 300s, configurable via
  `--wallet-exhausted-cooldown-secs`) skips CLOB attempts for
  subsequent placements until it elapses; real money resumes
  automatically when the wallet frees up (e.g. when an existing
  position settles). The session summary adds
  `paper_fallback_placed`, `paper_fallback_total_stake`,
  `paper_fallback_wallet_exhausted_events`,
  `paper_fallback_wallet_exhausted_last_at`, and
  `paper_fallback_last_reason` so the audit trail makes wallet
  exhaustion visible in the daily review without polluting real-money
  P&L. Two new fields on `LiveBetRecord` (`placement_mode`,
  `paper_fallback_reason`) so analysis scripts can split real vs.
  paper outcomes for ROI math while still using all bets for signal
  / fill-model training. 14 new tests; 34 total.
- **Stake-scaling promotion analyzer (Active #6 part 2 prep)** *(2026-05-13)* --
  new `scripts/analysis/analyze_stake_scaling_promotion.py` reads
  filled+settled bets that carry `calibrated_stake_multiplier` from the
  per-date session JSONs, buckets them into low/mid/high terciles, and
  emits a `need_more_data` / `hold` / `promote` verdict for the
  shadow-to-enforce decision. Promotion gate: ≥ 30 sessions of shadow
  data AND high-multiplier cohort beats low cohort by ≥ 5pp WR AND
  ≥ 5pp ROI. Bucket boundaries use inclusive cuts on both sides so
  bimodal data (most bets clamp to 0.5x floor or 1.5x ceiling) routes
  to low/high cleanly instead of collapsing into mid. Output:
  `data/analysis_output/stake_scaling_analysis/stake_scaling_analysis.{json,md}`.
  Wired into the daily refresh and surfaced as a new section in the
  weekly drift rollup HTML (verdict KPI + progress bar + per-bucket
  WR/ROI table). Current verdict on 11 filled bets across 2 sessions:
  `need_more_data` (2/30 sessions); high cohort already trending the
  right direction (+9.5pp WR, +1.7pp ROI vs low) but well within noise
  at this sample size. 10 new tests; 33 total.
- **Weekly drift HTML rollup -- Active #5 closed** *(2026-05-13)* --
  `scripts/analysis/build_weekly_drift_rollup.py` renders a one-page,
  trailing-N-day (default 7) HTML rollup from the per-date
  `daily_human_review/*_human_review.json` files. KPI bar (window P&L /
  ROI / fill rate / filled WR / filled-bet count / active alert count),
  alerts feed (newest first, color-coded by dimension), eight inline-SVG
  sparkline panels (daily ROI, cumulative P&L, fill rate, filled WR,
  filled bets/day, calibration alerts, regime-mix max TVD, reconciler
  recovered share), and a per-day detail table. Pure stdlib, no JS, no
  third-party deps. Wired as the penultimate step of `run_daily_refresh.py`
  so the artifact stays fresh after every session. **Critical fill-rate
  semantics:** uses `fill_rate_health.today.placed` (attempt
  denominator) NOT `session_summary.orders_placed` (CLOB-success), so
  wallet-balance error days surface clearly in the KPI bar -- caught
  during smoke testing on the 2026-05-12 session where the wrong
  denominator would have shown 88.9% fill rate hiding the real 11%.
  Output: `data/analysis_output/weekly_rollup/<end_date>_weekly_rollup.html`
  + canonical `weekly_rollup.html`. 10 new tests; 32 total.
- **Stage-2 promotion alert wired up** *(2026-05-13)* -- the
  `model_freshness_health` inline handler's `_stage2_validation_brier`
  was silently returning `None` for both production and staging because
  it scanned `payload["lines"]` with sub-key `validation_brier`/`brier`
  -- the actual Stage-2 schema is
  `payload["validation_metrics"][<line>]["stage2_brier"]`. The drift
  alert had been a no-op since the Stage-2 staging-vs-production split
  shipped. Fixed; immediately surfaced a real promotion candidate:
  staging shows a 0.005 Brier improvement over production (5/6 lines
  improve, O11.5 marginally regresses; weight-selection changes look
  substantive but should be tracked over multiple daily refits before
  promoting).
- **EV-policy missing-features warning resolved** *(2026-05-13)* --
  `LogisticJsonScorer.missing_input_cols` was conflating two different
  conditions: (a) "column key absent from row" (real schema gap, must
  warn + fail enforce) vs (b) "column present, value is `None`" (e.g.
  `current_state_value_base_empirical` is legitimately `None` for
  no_score_drift candidates which take a different state-value lookup
  path; the artifact's median imputation handles this correctly). Fix:
  flag only structurally-absent columns; let value-null pass through to
  median imputation silently. Yesterday's session would have dropped
  `ev_policy_missing_runtime_features` from 6 to 0; enforce mode is now
  safe for known-nullable features without blanket-failing. 8/8 EV-policy
  tests still pass; the safety case (empty `{}` row) is still covered.
- **Stale notes block in refresh manifest** *(2026-05-13)* -- the
  hardcoded `notes` list in `run_daily_refresh.py` still claimed
  "Refresh excludes live decision artifacts by default: calibration and
  EV-policy model JSONs are explicit research promotions" -- contradicts
  the 2026-05-12 in-band retrain shift. Updated three notes to reflect
  current behavior (in-band decision-artifact retraining, Stage-2
  staging-vs-production split, 32-step pipeline shape). Future agents
  reading the manifest will no longer be misled.

These were live items in earlier versions of this roadmap and are now built
or wired. They still need *use* (running the harness, watching the new
ledger rows), but the code exists.

- **Walk-forward harness scaffolding** -- `scripts/analysis/walk_forward_runner.py`
  (score-event) and `no_score_drift_walk_forward.py` (no-score, kept
  separate -- do not pool families) exist with pytest coverage. What's
  outstanding is *running* them on post-TR20+TR21 data; see Active #1.
- **Feature-lineage fields in live ledger** -- `entry_ask`, `decision_ask`,
  `posted_limit`, and `actual_fill_price` are written immutably; the
  `live_orders_ledger.jsonl` and unified signal table both carry them.
- **Candidate-universe training data** -- monitor + signal pipeline emit
  trade, skip, skip-with-features, and shadow no-score drift rows; rollups
  land under `data/.../candidate_universe/` and feed
  `build_signal_training_table.py`.
- **Queue-aware execution replay** -- `build_queue_aware_execution_replay.py`
  compares current limit vs. +1c / +2c / taker-like fills by realized EV.
- **Daily observability rollup** -- `build_daily_human_review_report.py`
  produces a per-date decomposition; serves as the proto-dashboard
  (Active #5 fills the alerting gap).
- **Stage-3 v2 (TR20) and Stage-2 density_alt + hr_factor (TR21)** --
  promoted to live. Empirical TR1-TR21 calibrations may now be stale; see
  Active #1.
- **Orphan-fill reconciliation** *(2026-05-11)* -- new
  `scripts/trading/live_reconciliation.py` queries the public Polymarket
  data-api by wallet on startup and shutdown to recover fills the CLOB
  SDK's maker-address-filtered `get_order` / `get_trades` paths miss.
  Patches the `LiveBetRecord`, clears the false cancel, and writes a
  `_event="reconciled_filled"` row. Caught the 2026-05-10 MIN@CLE 7.5
  miss in regression. Also collapsed the "canceled" vs. "cancelled"
  status normalizer bug that let cancelled orders linger in `_open_orders`
  past stale-order timeout.
- **Observability cleanups** *(2026-05-09 to 2026-05-11)* -- monitor
  module split into ten files <1k LOC each (LLM-friendly); tick-buffer
  health WARN gated on `active_games` (prevents 30-min spam after games
  end); startup-refresh plan-only manifests now write a separate summary
  line; `live_engine.py` log rotation (gzip after 1 day, retention 60
  days) added.
- **Daily refresh as canonical** *(2026-05-08)* -- startup refresh now
  bundles scrape + team_game_log + preflight + `park_hr_factors` step;
  `run_daily_refresh.py` is the one entry point.
- **Calibration / probability anomaly fixes** *(2026-05-11)* -- two
  anomalies that blocked probability-based promotions are fully
  resolved. (1) The runtime calibrator was loading an artifact with
  `selected_method=identity` (built 2026-04-26 on 27 rows, validation
  split 3/3 positive). Fixes: taught `calibrate_signal_probabilities.py`
  to read the `calibration_opportunity_training_table.jsonl` schema;
  added an identity-rejection guard that overrides the validation-logloss
  winner when train ECE shows a challenger is materially better
  calibrated (default 0.05 gap); wired the build into `run_daily_refresh.py`;
  added a startup WARN when the loaded calibrator is identity. After
  rebuild on 638 rows, `score_event_transition` selects Platt (raw 0.97
  -> 0.73) and `no_score_drift` selects isotonic. (2) The
  `score_confirmed_60s_raw_fv` maturity-report metric was a category
  error pairing the over-game FV against the 60s score-confirmation
  label (AUC 0.372 -- worse than chance by construction). Replaced with
  `score_confirmed_60s_proxy` using `shadow_p_score_event_proxy` (AUC
  0.911 on the same data). End-to-end validation that the next live
  session emits non-identity `fair_value_calibrated` rows is the only
  remaining check, and happens automatically.
- **Calibration-drift alerting in daily review** *(2026-05-11)* --
  `build_daily_human_review_report.py` now carries a
  `calibration_health` block + a "Calibration Health" markdown section.
  Three independent signals (artifact metadata, sampled per-family
  candidate-row deltas, day-over-day method changes) feed an alert
  list that mirrors into the Notes block. Catches the failure mode
  that hid for weeks: even with only 13 sampled rows, a 100%-identity
  method count fires a load-time alert. Re-running on 2026-05-10 data
  cleanly surfaces "13/13 rows used calibration_method=identity --
  runtime calibrator was a no-op." This is the load-time complement
  to the fit-time identity-rejection guard.
- **Fill-rate + signal-quality drift alerts** *(2026-05-11)* --
  `build_daily_human_review_report.py` now carries `fill_rate_health`
  and `signal_quality_health` blocks plus a "Drift Health" markdown
  section. Both compare today against a trailing 7-day baseline (live
  vs paper kept separate); fires on a >=20pp drop in fill rate or
  filled win rate (with min today/baseline samples) and on zero-fill /
  zero-win days at n>=5. Catches execution-side regressions like the
  orphan-fill bug: had this been live, a fill-rate drop would have
  surfaced on the day the bug started instead of weeks later.
- **Regime-mix shift alert** *(2026-05-12)* -- third drift dimension
  in the daily review. Computes per-day placed-bet distributions across
  ask-bucket, current-state-edge band, and phantom-risk band, persists
  them into the daily-review JSON, and compares today's distribution
  against the pooled trailing-window baseline via Total Variation
  Distance. Fires when TVD >= 0.30 on any dimension (with min today/
  baseline samples). Catches the failure mode where outcome metrics
  look fine in aggregate but the bot is suddenly trading a different
  cohort -- a regime shift that win rate would only flag *after* a
  losing streak. The four-alert family (calibration / fill / signal /
  regime) is the full Active #5 per-day surface area; the remaining
  Active #5 work is a weekly HTML rollup.
- **Learned execution-policy prototype** *(2026-05-12)* -- new
  `scripts/analysis/learn_execution_policy.py` consumes the existing
  queue-aware replay output (71 distinct bets) and quantifies the
  headroom available from a smarter posting-offset choice. Findings on
  the realistic `queue_adjusted` fill model:
  - **Baseline** (`current_limit`, today's default): mean per-bet ROI
    **-4.52%**, 55% fill rate, **-$45.25** total realized over the
    window.
  - **Always `taker_like`**: mean per-bet ROI **+2.06%**, 100% fill
    rate, **+$65.33** -- a swing of **~$110** of realized P&L over the
    same 71 bets at $20 stake. Robust under LOOCV.
  - **Oracle** (best policy per bet, hindsight): mean ROI **+10.81%**;
    headroom over baseline is **+15.33pp**. The simplest non-baseline
    rule (always `taker_like`) captures **42.9%** of that headroom.
  - **Cohort signals** that flip the optimal policy: for ask >=0.70
    `taker_like` beats by +12pp; for ask 0.55-0.70 `current_limit` is
    still best. Spread-wide regimes also favor `taker_like`. Cohort
    lookups overfit at n=71 (`learned_by_ask_bucket` LOOCV ROI -4.38%
    vs in-sample +2.66%) -- confirms the rule should stay simple until
    a larger sample is available.
  - Prototype is research-only; promotion needs walk-forward evidence
    with score-event and no-score-drift families kept separate.
  - Wired into `run_daily_refresh.py` so the artifact refreshes after
    each session -- no manual rerun. Active #7 follow-ups now just wait
    for the daily-built artifact to reach a meaningful sample size.
- **Daily refresh fully owns observability** *(2026-05-12)* -- every
  per-day artifact (candidate universe, calibration build, model
  maturity, FV stage ablation, unified signals, training tables,
  execution diagnostics, queue-aware replay, learned-execution policy
  prototype, state-value transition report, no-score drift, walk-
  forward, daily human-review with calibration/fill/signal-quality/
  regime-mix drift alerts AND orphan-fill reconciler tracking) is now
  a `RefreshStep` in `run_daily_refresh.py`. Operators no longer need
  to remember any post-session command -- the startup script is the
  one entry point. README's operational guidance updated to match.
- **AGENT_CONTEXT.md refresh + Stage-3 weight externalization +
  refresh smoke test** *(2026-05-12, Tier 2 hardening)* -- three
  documentation/architecture pieces shipped together. **(a) Per-folder
  agent docs updated** for the work since 2026-05-08:
  `MASTER_CONTEXT.md`, `scripts/trading/AGENT_CONTEXT.md`, and
  `scripts/analysis/AGENT_CONTEXT.md` now reflect the
  orphan-fill reconciler, correlated-line cap, calibrated stake
  scaling, refactored `live_engine.py` impl modules, 35-step refresh
  pipeline, drift-alert surface, and Wilson UB gating. **(b) Stage-3
  weights externalized** via a new optional
  `cache/team_offense_v2_weights.json` -- `TeamOffenseModel.load()`
  reads betas + shrinkage from this file when present, otherwise uses
  the compiled-in defaults. New `scripts/analysis/promote_team_offense_v2.py`
  reads the daily-refresh research artifact (`phase4_models.json`)
  and writes a validated production weights JSON. Promotion stays
  manual but the *path* is now non-code-change, which unlocks future
  auto-promotion gates. 11 new tests cover the override paths +
  promotion. **(c) Refresh smoke test** -- new
  `tests/test_run_daily_refresh_smoke.py` walks the full daily-refresh
  pipeline with subprocesses stubbed out, asserting the manifest
  shape, health-rollup output, staleness-skip behavior, and
  graceful failure handling. Catches wiring regressions like the
  bogus `inline_handler=` field I tripped on earlier this session.
- **Stake scaling by calibrated confidence** *(2026-05-12, Active #6 part 2)*
  -- `live_engine_placement.py` now multiplies base stake by a [min, max]
  multiplier derived from the *calibrated* edge
  (`fair_value_calibrated - decision_ask`), with `--calibrated-stake-scale-mode`
  defaulting to **shadow** (computed + recorded on the bet, stake not
  changed). Ramp: linear from min at edge=0 through max at
  `--calibrated-stake-ramp-top-edge` (default 0.15). Default bounds
  0.5x-1.5x base stake. Five new audit fields on `LiveBetRecord`
  (`calibrated_stake_mode`, `calibrated_stake_base_stake`,
  `calibrated_stake_multiplier`, `calibrated_stake_edge_used`,
  `calibrated_stake_applied`). Promote to enforce after ~30 shadow
  sessions of evidence that the multiplier moves in the right direction
  on filled outcomes.
- **Smart retrain skip** *(2026-05-12)* -- expensive subprocess steps
  (Stage-2 staging, Stage-3 features/calibration table/fit, EV-policy
  win+fill models, EV-policy backtest, walk-forward) now carry a
  `staleness_check` policy. The step is skipped (`status="skipped_fresh"`)
  when its declared output is newer than every declared input file plus
  any directory in `input_dir_mtime_roots` (used for huge corpora like
  `data/games/regular/` where leaf-file globbing is expensive). New
  `--force-retrain` CLI flag bypasses every check. On a back-to-back
  refresh today the Stage-2 step **skipped instead of paying 956s**
  (~16 min), confirming the design works against real data; the same
  refresh that previously ran in 24 min now runs in ~6 min when nothing
  has changed.
- **Wilson-interval drift gating** *(2026-05-12)* -- the daily
  human-review's fill-rate and signal-quality drift alerts now gate
  point-estimate drops on a Wilson upper-bound test
  (90% one-sided, z=1.645). An alert fires only when the trailing
  baseline rate exceeds today's Wilson UB -- i.e. even being generous
  about today's small-sample noise, today is *statistically* below
  baseline. Prevents the canonical false positive "1/3 = 33% fill rate,
  alert!" against an 87% baseline. Existing tests verified that real
  drops (1/5 vs 87.5%) still fire because the Wilson UB at 1/5 is ~0.51,
  well below baseline.
- **Daily refresh now self-corrects on new data** *(2026-05-12)* --
  expanded `run_daily_refresh.py` from 25 to **35 steps** so every model
  the live engine consumes is rebuilt from the latest data each day, and
  any meaningful drift surfaces as a refresh-time alert. New steps:
  - `train_baseline_models` + `ev_policy_backtest` -- the EV-policy win
    and fill models the runtime references were previously locked-in
    research artifacts; they now refit on every refresh so shadow EV
    scoring stays meaningful as fresh data comes in.
  - `stage2_run_env_retrain_staging` -- Stage-2 retrains from the full
    corpus every refresh, writes to `cache/mlb_stage2_run_env.staging.json`
    (NOT the production cache). Auto-rebuild is safe; auto-promotion
    needs human review.
  - `stage3_team_offense_features` + `stage3_team_offense_calibration_table`
    + `stage3_team_offense_v2_fit` -- the three-step Stage-3 v2 retrain
    chain. Output is a research artifact (`phase4_models.json`); the
    production weights live in `team_offense_model.py` and require an
    explicit promotion.
  - `fv_gap_decomposition` + `calibration_market_anchored_alpha` -- added
    2026-05-13 to diagnose why FV separates from market and to train
    family-separated, market-anchored residual alpha models from calibration
    opportunities.
  - `model_freshness_health` -- inline handler that (a) diffs Stage-2
    staging vs production validation Brier and alerts on a >= 0.001 (0.1pp)
    drift, and (b) age-checks every model artifact, alerting when one
    is > 30 days old.
  - `refresh_health_rollup` -- end-of-refresh operator summary that
    reads the per-step results, the latest daily human-review alert
    counts, the walk-forward summary, and the model-freshness notes,
    then prints one consolidated block at INFO so "is the project
    healthy?" is answerable without opening any file.
  After this, the only manual work left in the model lifecycle is
  promoting Stage-2 / Stage-3 / gate thresholds -- everything else
  refits and self-audits each day.
- **live_engine.py refactor for LLM friendliness** *(2026-05-12)* --
  the engine file grew to 2,021 lines after the cap + reconciliation
  work; split into four focused modules to keep every file under the
  1,200-line LLM-friendly threshold. `live_engine_cli.py` (CLI args +
  defaults, 287 lines), `live_engine_session_io.py` (session JSON +
  lifecycle ledger writers, 350 lines), `live_engine_placement.py`
  (the `_place_bet` execution path, 464 lines), `live_engine_setup.py`
  (logging, log rotation, startup-refresh glue, 224 lines).
  `live_engine.py` itself is now **1,103 lines** -- just the
  `LiveTradingEngine` class as thin wrappers calling out to the impl
  modules, plus the `main()` driver. Back-compat preserved by
  re-exporting every constant via `from live_engine_cli import ...`;
  existing imports keep working. 247/247 tests pass.
- **Correlated-line exposure cap** *(2026-05-12, Active #6 part 1)* --
  `live_engine.py` now refuses to place multiple over-side bets on the
  same game when they share a correlated trade idea. Two rules:
  count cap (max 2 over-side bets per game) + spacing cap (lines must
  be >= 1.5 runs apart, so O7.5+O8.5 is blocked but O7.5+O9.5 is OK).
  Tunable via `--max-correlated-over-lines-per-game` and
  `--min-correlated-line-gap`. Audit of pre-cap data confirmed the
  risk: **10/62 game-sessions** had multi-line placements; 5 of those
  filled multiple correlated bets ($20-$30 of effective same-trade
  exposure per occurrence). With defaults, the canonical 2026-05-08
  O7.5+O8.5 placement would have been blocked at the second line.

## Active priorities

1. **Run the post-TR20+TR21 walk-forward and re-certify enforced gates.**
   *Top priority.* Every enforced threshold (`gate_extreme_edge=0.22`,
   `edge_threshold=0.10/0.15`, inning floors, FV/ask gap, blowout-relax
   bands) was tuned on v1 Stage-3's edge distribution. Without a fresh
   walk-forward, the entire enforced gate stack is unproven against the
   live model.
   - **Report-builder shipped 2026-05-13**:
     `scripts/analysis/build_walk_forward_certification.py` produces the
     per-cohort + per-gate scorecard with auto-degrading verdicts, wired
     into the daily refresh. On day-30 you re-read
     `data/analysis_output/walk_forward_certification/walk_forward_certification.md`
     instead of designing the report under deadline.
   - **Outstanding**: 30+ trading days post-2026-05-07 of accumulated
     filled bets (currently 53 / 23 dates -- `INSUFFICIENT` label;
     readiness flips to `READY` at 150 filled bets / 30 dates,
     expected ~2026-06-10 if pace holds).
   - **Then**: read the verdicts, cross-check the recommended
     thresholds against the cohort-band evidence, and apply
     keep/retune/retire decisions per enforced gate.
   - Files: `scripts/analysis/build_walk_forward_certification.py`,
     `walk_forward_runner.py`, `build_signal_training_table.py`,
     `train_baseline_models.py`, `backtest_ev_policy.py`.

2. **Watch the orphan-fill reconciler and decide whether the data-api
   path becomes primary.** The reconciler is fail-open; we need to
   measure how often it fires vs. the CLOB SDK's normal fill path. If
   reconciliation is recovering >10% of real fills, the SDK path is the
   exception, not the rule, and the live engine should query the data-api
   first instead of as a safety net.
   - Tracking is now automated: the daily human-review report carries
     an "Orphan-Fill Reconciler" section with per-source counts and
     fires an alert when recovered_share >= 10% (with min sample 3
     filled). Just read the daily-review markdown -- no manual ledger
     scraping needed.
   - Promote primary-source change only with a written justification
     across ~30 sessions of evidence.
   - Files: `scripts/trading/live_reconciliation.py`,
     `polymarket_client.py`, `live_order_lifecycle.py`.

3. **Promote shadow signals to enforced gates -- only with walk-forward
   support.** Two candidates have the strongest pre-TR20 evidence and are
   the natural first promotions if Active #1 confirms them on fresh data:
   - `gate_ask_max=0.85` -- block bets at very high implied probability
     where Winner's Curse dominates; cancelled-bet WR (95.5%) vs filled
     WR (60%) gap concentrates here.
   - `current_state_edge_min >= 0.05` -- HOU@BOS O9.5 (2026-05-02) and
     similar: negative current-state edge persistently loses regardless
     of FV.
   - Both stay shadow until the walk-forward says go.
   - Files: `scripts/trading/signal_pipeline*.py`, `signal_config.py`.

4. **Move the trade rule to realized EV with state-value guardrails.**
   Calibrated probabilities are now trustworthy (see Recently Completed);
   the remaining blocker is fresh fill-rate curves from Active #1.
   Replace `decision = (edge > threshold)` with
   `decision = (p_fill * EV_if_filled - cost) > 0`, plus current-state
   edge / phantom-risk guardrails. Already structured in
   `live_ev_policy_runtime.py` as a shadow scorer.
   - Files: `scripts/analysis/backtest_ev_policy.py`,
     `scripts/trading/live_ev_policy_runtime.py`,
     then `live_engine.py` for promotion.

5. **Risk controls tied to model uncertainty.** *Both code pieces
   shipped 2026-05-12; promotion gate analyzer shipped 2026-05-13;
   promote CLI shipped 2026-05-15; auto-actuation shipped 2026-05-16
   via the runtime-overrides config layer.* The correlated-line
   exposure cap (part 1) and stake scaling by calibrated confidence
   (part 2) both landed. Stake scaling still runs in shadow by default
   -- the multiplier is computed and logged on every bet but doesn't
   actually change stake. The promotion-gate analyzer
   (`scripts/analysis/analyze_stake_scaling_promotion.py`) refreshes
   daily and emits a `need_more_data` / `hold` / `promote` verdict
   based on bucketed filled-bet outcomes -- check the weekly rollup's
   "Active #6 stake-scaling promotion" panel for current status. When
   the verdict reads `promote` (≥ 30 sessions, high cohort beats low
   by ≥ 5pp WR AND ≥ 5pp ROI), the daemon will auto-promote in
   `--auto-daemon-mode act` (subject to the 14-day cooldown), or run
   `python scripts/analysis/promote.py stake-scaling` manually.

6. **Learned execution policy -- replace the fixed spread heuristic.**
   Offline prototype landed 2026-05-12 (`learn_execution_policy.py`,
   see Recently Completed). The replay shows ~$110 of realized P&L
   headroom over 71 bets just from switching from `current_limit` to
   `taker_like`, with the cleanest signal being "ask >= 0.70 -> take,
   ask < 0.70 -> keep current limit". The prototype reruns automatically
   as a step in `run_daily_refresh.py`, so the artifact stays fresh as
   the bet sample grows -- no manual refresh needed. Outstanding to
   promote to live:
   - Wait for the daily-refresh artifact to reach a meaningful sample
     (~200+ bets, expected ~2026-06-15 if pace holds) before
     re-evaluating; `learned_by_ask_bucket` may stabilize past that
     point.
   - Decide the live form: either (a) hardcode the simplest rule
     (`taker_like` when `decision_ask >= 0.70`, else `current_limit`)
     behind a feature flag, or (b) plumb the learned lookup into
     `live_pricing.py` and pick per-bet. (a) is lower-risk to ship.
   - Active #2 (orphan-fill / data-api primary) may reshape what "the
     order reached the book" means, so wait on its conclusion before
     committing to a live execution-policy promotion.
   - Files: `scripts/trading/live_pricing.py`, `live_engine.py`. The
     prototype artifact lives at
     `data/analysis_output/execution_policy_prototype/learned_execution_policy_report.{json,md}`
     and refreshes daily.

7. **No-score drift promotion path.** Continue paper-only with
   `evaluate_no_score_drift_policy.py` and the dedicated
   `no_score_drift_walk_forward.py`. Promotion gate: durable positive
   ROI on empirical-support segments across 60+ post-TR20 days, no
   pooling with score-event family.
   - Files: `scripts/analysis/no_score_drift_walk_forward.py`,
     `evaluate_no_score_drift_policy.py`.

8. **Stage-2 / Stage-3 fresh-test calibration audit (Phase 6).** Rebuild
   Stage-3 v2 + Stage-2 (density_alt, hr_factor) on 2026-05-07 onward
   once ~30 days of post-TR20 games are in the corpus, validate against
   a held-out slice, and confirm the family weights chosen by the
   validation tuner haven't drifted.
   - Files: `cache/build_mlb_stage2_run_env.py`, `model_improvements/`
     Phase 6 notes (to be written), training corpora under `data/games/`.

9. **Per-cohort calibration drift detection.** Today's
   `calibration_health` is aggregate -- a model that is well-calibrated
   on average but mis-calibrated in one cohort (e.g. high-ask, late-
   innings, score-event family only) will pass the existing alert
   while quietly bleeding money on the bad cohort. Mirror the
   `cohort_roi_health` decomposition (edge / ask / inning / line /
   current-state-edge bucket) onto calibration: per-cohort Brier +
   reliability-curve deviation against the aggregate calibrator.
   Fires when any cohort's reliability gap exceeds aggregate by >=
   2x AND has >= 30 samples. Becomes the 8th drift dimension; same
   Notes-block mirroring as the other seven.
   - Files: `scripts/analysis/build_daily_human_review_report.py`
     (new `cohort_calibration_health` block);
     `calibrate_signal_probabilities.py` (per-cohort reliability
     decomposition helper).

10. **Bet-level loss attribution.** Currently every bet has an
    outcome (won/lost) and a P&L, but when we lose we cannot answer
    "which model component owned this loss?" -- Stage-1 (base rate
    wrong), Stage-2 (park/weather wrong), Stage-3 (team offense
    wrong), calibration (FV->p mapping wrong), execution (filled at
    wrong price). For every settled bet, compute the marginal
    contribution of each model component to the realized error
    (decompose `actual_total - fair_value_uncalibrated` into stage
    contributions; decompose `fair_value_calibrated - 0.5` into the
    pieces calibration shifted). Aggregate over the trailing window
    so the daily review shows "X% of losses attributable to Stage-3,
    Y% to calibration, Z% to execution slippage." Tells the operator
    *which lever to pull* on a bad week instead of guessing.
    - Files: new
      `scripts/analysis/build_loss_attribution_report.py`; consumed
      by `build_daily_human_review_report.py` (new
      `loss_attribution_health` block).

11. **Counterfactual gate-change logger.** Today, deciding whether
    a gate threshold change is good requires waiting for a 30-day
    walk-forward. But for every candidate evaluated, we already have
    enough information to compute "would this candidate have been
    bet under threshold X' instead of X?" -- it's just a
    re-evaluation of the gate predicate. Persist a sparse
    counterfactual log per candidate: `{candidate_id, gate_name,
    current_threshold, alt_thresholds: [(thr, would_bet, edge,
    p_fill_estimate), ...]}`. On settlement, the outcome is known,
    so we get the realized P&L of each alternative threshold
    *without paper-trading them*. Operator gets near-real-time gate
    A/B telemetry that today takes a full walk-forward cycle.
    - Files: `scripts/trading/signal_pipeline_postfv_gates.py` (emit
      counterfactual rows); new
      `scripts/analysis/build_gate_counterfactual_report.py`.

12. **Settlement-truth verification.** *Shipped 2026-05-17.* See
    the Recently Completed "Active #12: settlement-truth
    verification" entry for full details. Cross-checks every
    settled bet against the MLB Stats API ground truth (final
    total from `linescore.teams.home.runs + .away.runs`) and
    classifies into 7 result codes (`ok`, `resolution_mismatch`,
    `total_mismatch`, `stale_filled`, `game_not_final_yet`,
    `missing_mlb_data`, `not_yet_settled`, `not_filled`). Wired
    as a refresh step before the per-date daily review so the
    `settlement_truth_health` block reads a fresh artifact.
    First production run on 89 filled bets: 60 OK, 0 ROI
    corruption, 7 game_not_final_yet (scraper-timing snapshot),
    22 missing MLB JSONs (24.7%, above alert threshold --
    surfaces a real data-refresh gap).

13. **Fast Wilson-UB demotion path.** *Shipped 2026-05-17.* See
    the Recently Completed "Active #13: fast Wilson-UB demotion"
    entry for full details. Parallel to the existing 14d windowed
    check; fires when N >= 20 post-promotion fills show a Wilson
    upper bound on win rate below the mean entry_ask (breakeven)
    at 95% one-sided confidence. Daemon bypasses the standard
    cooldown for `fast_demote` actions -- the whole point is to
    react in 5-6 days, not 14+. Symmetric `fast_promote` is
    intentionally NOT added: promotions can wait, demotions
    cannot.

## Hygiene (not blocking, but accumulating debt)

14. **Backup retention policy + PSI-history GC.** Each Stage-2 /
    Stage-3-v2 promotion writes a `<file>.prior_promote.json`
    sidecar; demote restores from it. Today nothing cleans them up,
    so on a long promotion cadence the cache folder accumulates
    stale backups (currently only 1 each, but the system runs
    indefinitely). Similarly `psi_history.jsonl` is append-only at
    7 features/day = ~2.5k rows/year, manageable for now but
    unbounded. Decision: keep the trailing-N backups per lever
    (default N=5) and trim psi_history at trailing 365 days
    (drift-in-drift only uses trailing 30d anyway). Tests verify
    no demote-target is ever GC'd.
    - Files: `cache/AGENT_CONTEXT.md` (policy doc),
      `scripts/analysis/promote.py` (backup-GC on promote),
      `scripts/analysis/build_concept_drift_report.py` (psi_history
      trim on append).

15. **"Shipped today, took effect tomorrow" operator doc + lag
    tracker.** Every promote.py file-swap changes the cache file,
    but the live engine loads that file at *next session boot*.
    Operators have asked "is this in effect?" enough times to
    deserve an explicit answer. Add: (a) a one-page doc in
    `docs/operational/promotion_lag.md` explaining the
    promote-time vs. effect-time gap per lever; (b) a refresh-time
    check that compares each cache file's `mtime` against the last
    live-engine startup time and reports "your stage2 promote
    will take effect on next live boot" as a Notes-block hint.
    - Files: new `docs/operational/promotion_lag.md`;
      `build_daily_human_review_report.py` (new
      `promotion_lag_health` block).

16. **Model lineage tracking.** Every promoted artifact today
    records `created_at`, but not "git sha of the builder that
    produced this" or "hash of the input dataset." When a
    regression surfaces and the team asks "what changed between
    the model that worked and the one that doesn't?", we have to
    grep git log against approximate timestamps. Add
    `lineage: {git_sha, dataset_hash, builder_path}` to the
    promote audit row and to the artifact JSON itself; surface in
    `promote.py status`. Cheap to add, expensive to backfill
    later.
    - Files: `scripts/analysis/promote.py` (lineage capture),
      `cache/build_mlb_stage2_run_env.py` +
      `promote_team_offense_v2.py` (write lineage into artifact),
      `live_engine_setup.py` (log loaded artifact lineage at
      startup).

## Bidirectional trading -> market-making (long-horizon)

The bot today is **Over-only**: it evaluates one side of every game,
quotes one direction, and books P&L on directional accuracy. The
long-horizon ambition is to become a **two-sided "smart market maker"**
on Polymarket MLB OU markets -- quote both bid and ask on every game,
turn a small profit per side through high volume + spread capture, and
use inventory management to stay roughly delta-neutral on outcomes
where our edge is in *spread* not *direction*.

This is a multi-quarter pivot, not a one-week task. Five phases (A-E).
Each phase has a meaningful checkpoint where we can stop and re-evaluate;
nothing past Phase A goes live until the prior phase has produced
durable evidence the same way Active #1 / walk-forward-certify gates
the current Over-only model.

**Strategic reason to do this**: at our current 3.4 fills/day rate the
self-improvement loop is starved of data; the model is technically
sound but underfed. Doubling the addressable signal universe (Over +
Under) and then quoting two-sided (taking spread, not just direction)
moves us from ~50 candidates/day to ~500+ -- enough volume that every
diagnostic, drift alert, calibration retrain, and walk-forward
certification matures in days instead of weeks.

### Phase A -- Symmetric UNDER signals (foundation, ~6 weeks)

**Shipped 2026-05-16** (offline / shadow only; live trading remains
Over-only until Phase C). See the Recently Completed "Phase A
foundation: UNDER offline pipeline" entry for full details. The
high-level state of each item:

A1. **UNDER-side book ingestion.** *Shipped* -- audit confirmed
    the monitor already polls both sides and signal_engine attaches
    `under_best_bid/ask/...` to the book payload; the original
    roadmap text was based on a stale assumption. Daily-review now
    surfaces `under_pair_available_rate` (~49% in production --
    Phase C needs to raise this for live UNDER quoting).

A2. **UNDER FV inference + candidate emission.** *Partially
    shipped*: separate UNDER calibrator artifact ships
    (`signal_win_calibration_under.json`, refreshed daily) with
    flipped labels + raw probs, separate stability gate. UNDER
    candidate emission in the LIVE signal pipeline is deferred to
    Phase B (it would touch the live trading runtime; foundation
    is meaningful without it because the candidate_universe table
    already carries the OVER candidate's full FV + ask + outcome
    which is what UNDER offline analysis needs).

A3. **UNDER state-value transition modeling.** *Shipped* --
    `build_under_state_value_transition_report.py` mirrors the
    Over report with flipped outcome, under-ask ROI math, and
    inverted regime classifiers.

A4. **UNDER walk-forward certification.** *Shipped (narrower
    scope than originally specced)*. Per-gate scorecard
    intentionally omitted: no UNDER gates are enforced today, so
    "what threshold works" is premature. Per-gate piece lands in
    Phase C with the first UNDER gate. Sample-readiness +
    per-cohort scorecard + per-week drift all ship.

**Remaining for the Phase A -> B transition**:

A5. **(NEW) Live UNDER candidate emission**. Currently UNDER
    analysis runs against the OVER candidate's universe (every
    OVER candidate's under side). The selection bias means
    "UNDER win rate" reflects OVER-bot misses, not generalizable
    UNDER edge. Emit an UNDER candidate row alongside every OVER
    candidate in the live signal pipeline (paper-mode only at
    first), with its own FV (= 1 - over_fv applied through the
    UNDER calibrator) and its own gates. This is what enables
    Phase B side-aware drift alerts to compare like-with-like.
    - Files: `scripts/trading/signal_pipeline_postfv_gates.py`,
      `scripts/trading/probability_calibration.py` (load the
      `signal_win_calibration_under.json` artifact).

### Phase B -- Symmetric infrastructure (~3 weeks, after A)

**Shipped 2026-05-17** (foundation + structure; UNDER trading is
still NOT enabled in the live engine -- that's Phase C). See the
Recently Completed "Phase B foundation: symmetric infra" entry for
full details. The high-level state of each item:

B1. **Side-aware drift alerts.** *Shipped (calibration_health
    only)*. `calibration_health` now reads the UNDER calibrator
    artifact and exposes a parallel `under` sub-block plus
    side-prefixed alerts (`under: ...`). The other six drift
    dimensions (fill_rate, signal_quality, regime_mix, cohort_roi,
    concept_drift, drift_in_drift) defer their splits to Phase C
    when real UNDER trading produces UNDER fill/outcome data;
    until then the splits would be all-zero / no-data and add
    noise without value. Concept_drift + drift_in_drift stay
    side-agnostic by design (input-feature drift).

B2. **Side-aware audit log + promote CLI.** *Shipped*.
    `PromotionEvent` carries a `side` field (default "both" for
    side-symmetric levers like stage2/stage3-v2). `promote.py`
    accepts `--side {over,under,both}` on stake-scaling and
    gate-threshold subcommands (default `over` -- today's live
    engine is Over-only). `latest_promotion_event_for_lever` gained
    an optional `side` filter that matches same-side OR `both`
    rows so daemon retrospective + drift attribution can filter
    correctly without losing side-symmetric promotions.

B3. **Side-aware session JSON + reports.** *Shipped (foundation)*.
    `BetRecord` already had a `side` field (defaulted "over").
    Daily review's `bet_totals` now exposes a `by_side: {over: {...},
    under: {...}}` sub-block with per-side count, filled, wins,
    losses, profit, ROI, win_rate. Today UNDER subtotals are
    all-zero (no UNDER bets placed); Phase C populates them
    naturally without further plumbing. Each compact bet row
    surfaces `side` for the markdown table.

B4. **UNDER paper-mode validation period.** *Documented*. Before
    any UNDER bet touches real money:
    - Phase C ships the live-engine UNDER candidate emission +
      two-sided quote engine.
    - Operator runs the live engine with `--under-mode paper`
      (Phase C CLI flag) for **>= 60 daily sessions**. Threshold
      matches the OVER walk-forward `READY` verdict's date floor
      (30 dates) doubled, because UNDER is in less-validated
      territory than OVER was when OVER promoted.
    - Across those 60 sessions, the daily review's `by_side.under`
      block must show:
      * `n_under_outcomes >= 150` (matches A4 walk-forward READY)
      * UNDER win rate within 5pp of the UNDER calibrator's
        predicted win rate (calibration is honoring outcomes)
      * UNDER taker ROI > 0% (positive expected value)
      * No persistent UNDER-side drift alerts from B1 (calibrator
        stable; cohort_roi will fire if outcomes diverge)
    - Only then can the operator flip from `--under-mode paper`
      to `--under-mode live`. This is the same gate OVER cleared
      in its first month; UNDER does not skip it.

### Phase C -- Market-maker foundation (~6 weeks, after B)

**Shadow shipped 2026-05-17** (compute + log only; no order
placement). See the Recently Completed "Phase C shadow: two-sided
quote engine foundation" entry. The live-engine flip (the part that
actually places two-sided orders) is gated by the B4 60-session
paper-mode validation milestone.

C1. **Two-sided quote engine.** *Shadow shipped*. New
    `scripts/trading/live_quote_engine.py` computes a per-tick
    `QuoteDecision` (bid + ask + skip reasons + hedge opportunity)
    when `--quote-engine-mode shadow` is passed. Writes to
    `data/{live,paper}_trading/quote_engine_shadow/<date>_quotes.jsonl`.
    The live-engine flip (`--quote-engine-mode act`) is deferred to
    after B4 paper-validation. Existing `_place_bet` path is
    untouched.

C2. **Inventory tracking.** *Shipped*. New
    `scripts/trading/inventory_tracker.py` aggregates
    `live_orders_ledger.jsonl` into per-game `GameInventoryRow`
    with `filled_over/under_shares` + `open_over/under_shares` +
    `net_over_shares`. Read-only by design: shadow quotes do NOT
    mutate the snapshot, so "what shadow quoted vs what really
    happened" stays uncontaminated. In Phase C shadow, the
    snapshot is cached once per session; Phase D adds tick-level
    refresh.

C3. **Inventory-aware quote shading.** *Shadow shipped (inside
    C1)*. The quote engine computes a signed shade =
    `(net_inventory / max_inventory) * max_shade` and applies it to
    BOTH bid and ask anchors. Positive when long Over (shifts both
    quotes DOWN to discourage adding + encourage flattening);
    negative when short. Clamped to ±max_shade. Default cfg
    `max_inventory_per_game=50, max_shade=0.05`.

C4. **Hedging on opposite-side opportunities.** *Shadow shipped
    (inside C1)*. The quote decision carries
    `hedge_opportunity: bool` + `hedge_side: "buy_under" | "buy_over"`
    + `hedge_target_price` + `hedge_max_price` + `hedge_reason`. Fires
    when net inventory exceeds 1 share AND opposite-side ask is
    at-or-below fair + hedge_premium (default 1c). Shadow only --
    no hedge order is placed. The shadow report aggregates triggers
    by side + inventory-at-trigger distribution.

### Phase D -- High-volume scaling (~4 weeks, after C)

D1. **Multi-game concurrent quoting at scale.** The current
    engine handles ~10 concurrent active games comfortably.
    Two-sided quoting on each ~doubles the order count. Profile
    the tick loop, the CLOB SDK rate limits, and the order-
    lifecycle bookkeeping for the new load. Likely requires
    batching order updates and a per-game tick-scheduling
    heuristic so we don't blow rate limits on a 15-game evening
    slate.

D2. **Per-game profit target tracking.** A market maker measures
    P&L per game, not per bet. New session JSON field:
    `per_game_pnl: {game_pk: {realized, unrealized, spread_captured,
    inventory_marked}}`. Daily review surfaces "X of N games
    were profitable today" as the headline KPI alongside the
    existing per-bet ROI. Target metric:
    `profitable_games_share` (% of games with realized P&L > 0).

D3. **Position-sizing across correlated games.** When inventory
    on multiple games correlates (e.g. all Over positions on a
    high-scoring-weather day), apply a portfolio-level cap. New
    `portfolio_risk_check` step in the candidate pipeline checks
    summed correlated exposure against a daily limit.

D4. **Risk limits (per game / per day / per cohort).** Three
    explicit caps the market-maker mode respects:
    `max_inventory_per_game` (absolute share count),
    `max_daily_drawdown` (kill-switch at -$X session loss),
    `max_cohort_inventory` (e.g. no more than $K exposure to
    high-ask cohort even across games). Mirrors the existing
    correlated-line cap pattern.

### Phase E -- Smart quoting (continuous improvement, after D)

E1. **Adverse-selection / toxic-flow detection.** When the same
    counterparty consistently hits our bids right before the
    market moves against us, that's toxic flow. Log fills
    against counterparty (where Polymarket exposes it) + against
    immediate post-fill book moves; build a "toxic counterparty"
    model that shades quotes wider when those counterparties
    are active. Defensive complement to C3 (inventory shading).

E2. **Dynamic spread sizing.** Today's spread is implicit
    (fixed offsets in `live_pricing.py`). A smart MM widens
    spread when volatility is high (high innings, big score
    changes) and tightens when volatility is low (early
    innings, quiet game). Spread function:
    `f(realized_volatility, time_to_resolution, inventory)`.

E3. **Time-decay-aware quoting.** A game in the 9th has minutes
    to resolution; a game in the 3rd has hours. The
    information-decay rate is different, and so is the
    inventory cost-of-carry. Tighten ask / loosen bid as
    resolution approaches and inventory needs to clear.

E4. **Cross-game model A/B harness.** Once volume supports it,
    run two variants of the quote engine on disjoint game
    cohorts (deterministic hash on `game_pk`). The existing
    self-improvement loop (drift alerts, daemon retrospective,
    promote/demote) is generalized to compare cohort-A vs
    cohort-B P&L per refresh. This is the natural follow-on to
    the daemon retrospective once paired-cohort data is
    available.

**Phase gating rule**: nothing past the current phase goes live
until the prior phase has accumulated 30+ sessions of clean
evidence the same way Active #1 / walk-forward gates the
current Over-only stack. The drift-alert family, calibration
stability gate, and daemon retrospective all generalize to
two-sided / market-maker mode -- we are extending the
self-improvement loop, not building a parallel one.

## Operational guidance (per session)

These are standing rules, not roadmap items -- they apply every day
regardless of which Active priority is currently being worked.

- **The daily refresh is the one entry point.** `run_daily_refresh.py`
  rebuilds every per-day artifact -- candidate universe, calibration
  opportunity table, calibration build, model maturity, FV stage
  ablation, unified signals, signal training table, execution
  diagnostics, queue-aware execution replay, learned execution policy
  prototype, state-value transition report, no-score drift policy
  evaluation, no-score drift paper ledger, walk-forward, and the
  daily human-review report (which carries calibration / fill-rate /
  signal-quality / regime-mix drift alerts). Do not run any of these
  builders by hand; if something seems missing from the refresh, add
  it as a `RefreshStep` instead.
- **Keep enforced gates stable between walk-forward checkpoints.** Do
  not retune gates off one or two sessions of live evidence; treat each
  gate change as a hypothesis that needs Active #1 to certify.
- **Read the daily human-review report after each session.** It carries
  the drift-alert surface (calibration / fill / signal-quality /
  regime-mix) and the `reconciled_filled` ledger summary. Active alerts
  surface in the Notes block at the top.
- **Preserve candidate-universe forensic detail.** Daily overviews use
  `*_candidate_rollup.json`; raw candidate rows remain full fidelity for
  trade/model-bearing audits, while repeated early pre-FV skips are sampled and
  should be audited from the rollup counts.
- **Maintain measurement integrity.** Live ledgers, candidate universe,
  session JSON, and analysis builders must agree on canonical field
  names and types. Prefer better diagnostics over more gates until the
  current pivot is measured cleanly.
