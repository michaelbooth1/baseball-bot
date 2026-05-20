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

Last roadmap review: **2026-05-19** (Band-gated calibrator
ENFORCE shipped: 2026-05-19 FV-overconfidence audit confirmed
**raw model is overconfident by +28pp at FV>=0.95** (487 settled
predictions: claimed avg 0.97, realized 0.70). Audit drilled into
the CLE@DET 7.5 LOSS (raw FV=0.979, base poisson=0.982 vs cell
empirical=0.893 on n=112 exact-match samples -- a 9pp gap that
production never sees because the cache is built in poisson-only
mode). Two fixes considered: global Alt-A promotion + global
calibrator enforce. Global Alt-A NOT shipped because the existing
shadow-override report flags a -23.8pp inning>=8 regression --
scoped (cohort-aware) Alt-A is the correct path and is now
roadmap'd as Active #17. Calibrator enforce shipped with a
band-gate: `DEFAULT_PROB_CALIBRATION_MODE` flipped from `shadow`
to `enforce` AND new `DEFAULT_PROB_CALIBRATION_ENFORCE_MIN_RAW
= 0.90` -- so the Platt calibrator now overwrites raw FV when
raw>=0.90 (captures the +28pp dangerous-tail correction) while
leaving the mid-band [0.80,0.90) alone (where the Platt fit
over-pulls by 10-16pp under realized). Per-candidate diag adds
`below_min_raw_kept_raw` + `enforce_min_raw_threshold` columns
+ new `below_min_raw_kept_raw` engine stat counter; session
params persist the new threshold for audit. CLE@DET 7.5 bet
would now calibrate ~0.78 (below 0.80 ask), skipping. Runbook:
`docs/operational/fv-recalibration-2026-05-19.md`. Roadmap
adds **Active #17** (Scoped Alt-A enforce -- cohort-aware
empirical override) + **Hygiene #18-#20** (line-5.5 high-FV
guard, mid-band calibrator refit, Negative-Binomial Stage-1
tail). Tests: 31 signal_engine + 59 live_execution all green
(one pre-existing golden-row test fails on an unrelated
unstaged DEFAULT_EXTREME_EDGE_MAX 0.22->0.17 working-tree
change). Earlier today: UNDER outcomes counterfactual
trailing-7d aggregate shipped: smooths the per-day block's noise
by walking the prior 6 dates + today, unioning settled rows, and
re-aggregating. Trailing alerts at n>=50 fire within ~3 sessions
of A5 emission opt-in instead of needing one "lucky" day to hit
n=30 alone; alert text mirrors via `(7d)` prefix to distinguish
window in the Notes block. Extracted 3 helpers
(`_collect_under_settled_rows`, `_aggregate_under_settled`,
`_under_settled_by_cohort`) so per-day and trailing share
arithmetic. The trailing alert also surfaces Phase B4 milestone
progress ("M/60 sessions of UNDER signal data accumulated") so
the operator sees the runway clock advance. **1383 tests + 41
subtests pass** (+10 from this ship). Earlier today: Phase A5
follow-up #2: UNDER outcomes counterfactual block shipped: closes the A5 ->
UNDER-coverage -> UNDER-outcomes observability trilogy. New
`_under_outcomes_counterfactual_health` daily-review block reads
`_candidates.jsonl` + `_outcomes.jsonl` (existing artifacts),
filters to `shadow_under` rows, settles UNDER wins (`final_total
< line`), and computes counterfactual P&L using paper-mode taker
math. 4-way status (`ok` / `no_shadow_under_candidates` /
`no_settled` / `check_error`), 5-dimensional per-cohort
breakdown, and 2 sample-size-gated alerts (profitable at >=+5%
ROI / unprofitable at <=-5% on >=30 settled). Mirrors via Notes
prefix `Under-outcomes:`. Also fixed a wart in
`_maybe_emit_under_candidate` where `decision_ask` was inheriting
OVER's value (cohort-by-ask would have been wrong). First run
against yesterday's session: `status=no_shadow_under_candidates`
(A5 emission flag shipped today; yesterday predates it).
**1373 tests + 41 subtests pass** (+14 from this ship). Earlier
today: Shadow-override report cohort breakdown shipped: slices Alt A's 30d bias-reduction
across 5 cohort dimensions (edge / inning / line / ask /
current_state_edge), surfaces top_cohorts (most_improved /
regressions / highest_coverage / largest_alt_b_savings), and
mirrors scoped-promotion + regression alerts to the daily
review via `_stage1_shadow_override_health`. **First production
run reveals a critical scoped-promotion case**: Alt A reduces
bias by +13.3pp on the negative-current-state-edge cohort (n=19,
53% coverage) but REGRESSES by -23.8pp on inning>=8 (n=7) -- a
global Alt A ENFORCE flip would significantly hurt late-inning
bets. The operationally correct path is now scoped ENFORCE on
cohorts where Alt A durably helps + leave production untouched
where it doesn't, instead of all-or-nothing. **1359 tests + 41
subtests pass** (+21 from this ship). Earlier today: Phase A5
follow-up shipped: new `_under_emission_health` daily-review
block closes the observability loop on this morning's A5 ship. Surfaces UNDER
coverage rate, decision breakdown (`shadow_under` /
`gate_min_edge` / `gate_no_under_liquidity`), price quality
(mean FV / ask / edge / calibration delta + FV histogram), 3-way
status (`not_emitting` / `no_liquidity` / `ok`), and 3 sample-
size-gated alert classes covering coverage gap + suspiciously
loose / tight UNDER gates. Mirrors via Notes prefix
`Under-coverage:`. First run against yesterday's session
correctly identifies `status=not_emitting` (operator hadn't
yet opted into shadow mode for 2026-05-18). **1338 tests + 41
subtests pass.** Earlier today: Phase A5 live UNDER candidate
emission shipped: closes the keystone item of the bidirectional
pivot's Phase A. New `--under-emission-mode {off, shadow}` CLI
flag on `live_engine_cli.py` opts the engine into emitting an
UNDER candidate row alongside every OVER candidate that reaches
the FV phase, with its own calibrated UNDER FV (via the
separately-trained `signal_win_calibration_under.json` artifact),
its own UNDER-side ask, and its own gate evaluation
(`decision=shadow_under` when UNDER gates pass; `gate_min_edge` /
`gate_no_under_liquidity` skip reasons otherwise). NO UNDER bets
placed in either mode -- pure observability so the paper-mode
runway accumulates UNDER signal-quality data the daily-review
`by_side` block, training table, loss-attribution, and shadow-
override reports all pick up automatically. Unlocks Phase B1
side-aware drift alerts (already shipped for `calibration_health`)
to compare OVER vs UNDER on like-with-like data; the eventual B4
UNDER paper-bet validation milestone becomes a config flip. Also
shipped earlier today: refresh `--mode live` hardcode fix on the
`unified_signals` + `signal_training_table` steps (paper bets now
propagate to loss-attribution + shadow-override; **12 paper bets
from yesterday's 2026-05-18 session now carry Alt-A diagnostics
in the training table end-to-end**, with the fill-aware steps
intentionally staying `--mode live`); Active #15 promotion-lag
tracker (closes the last open Hygiene item: new
`_promotion_lag_health` daily-review block compares each of the 5
promote.py levers' cache mtime against the most recent engine-boot
proxy (first-bet `placed_at` from the latest session file across
paper + live trading roots) and surfaces 5 statuses
(`effective_in_runtime` / `pending_next_session_boot` /
`cache_missing` / `no_session_history` / `check_error`) per lever.
Alert fires when a lever is pending for >24h (operator promoted but
forgot to restart engine). Mirrors via Notes prefix
`Promotion-lag:`. New operator doc at
`docs/operational/promotion_lag.md` explains promote-time vs
effect-time per lever, related daily-review blocks, and what the
tracker doesn't catch. **First live run: 0 alerts, stage1 pending
0.32h post-refresh, stage2 effective 240h, other 3 levers
`cache_missing` (first-time state).** Plus a small wiring fix:
unified-signal-table now propagates the 6 Stage-1 Alt-A shadow
fields from candidate rows to the master table (closes a gap
discovered during yesterday's paper-trading audit). **1312 tests +
41 subtests pass.** Hygiene section status: all 3 originally-listed
Hygiene items (#14, #15, #16 through v4) now closed. Earlier on
**2026-05-18**: Active #8 prep -- Stage-1 Alt-A
staging cache builder + refresh + daily-review surface shipped:
new `--smoothing-mode {poisson, empirical_when_available}` flag on
`cache/build_mlb_ou_cache.py` overwrites each cell's `poXX` with its
sibling `oXX` value, materializing the runtime's on-the-fly Alt-A
shadow as a real cache file. New `stage1_ou_cache_alt_a` refresh
step writes to `cache/mlb_ou_cache_alt_a.staging.json` (NEVER
auto-promoted; operator runs `promote.py stage1` after paper-mode
validation). New `_stage1_alt_a_staging_health` daily-review block
surfaces existence + age + override stats + cross-cache input-hash
divergence. **First production build: 4,200 of 4,298 cells (97.7%)
overridden, mean signed delta -2.74pp (Alt-A is conservative vs
production, the direction needed to reduce the +27pp bias), 21,240
(cell, line) overrides across 6 lines.** Completes the Active #8
scaffolding chain: loss attribution -> cell drill -> shadow-override
report -> runtime shadow logging -> promote.py stage1 -> staging
cache artifact. ENFORCE flip is now one CLI command after paper-mode
validation clears its bar. **1295 tests + 41 subtests pass.**
Earlier on **2026-05-17**: Active #16 v4 cross-artifact
consistency check shipped: new `compare_input_hash` helper in
`artifact_lineage.py` classifies each (artifact, input) pair as
match / stale / not_tracked / current_missing by comparing the
artifact's recorded `input_hashes[X]` to the current file hash.
New `_cross_artifact_consistency_health` daily-review block runs
over 10 known artifacts and surfaces both per-artifact stale
alerts AND cross-artifact divergence (two artifacts share an
input but recorded different hashes -- one was built before a
refresh, the other after). Mirrors via `Cross-artifact:` Notes
prefix. First production run: 0 alerts (5 artifacts match all
inputs; 4 pre-V2 awaiting next refresh; 1 missing). Earlier
same day: Active #14 backup retention
+ PSI-history GC shipped: promote.py's `_backup_prior_production`
now rotates the prior `.prior_promote.json` into a sibling
`<file>.prior_promote_archive/` directory under a timestamped
filename BEFORE writing the new backup, then GCs the archive to
BACKUP_ARCHIVE_KEEP=5 most-recent entries. Preserves the existing
"latest backup at .prior_promote.json" contract demote relies on
+ adds multi-promotion rollback history. PSI history file now
trimmed to PSI_HISTORY_RETENTION_DAYS=365 on every append +
corrupted lines get cleaned. Both pieces are best-effort
fail-open. Earlier same day:
`promote.py stage1` +
`demote stage1` subcommands shipped: closes the last gap in the
promote.py coverage matrix. Stage-1 was the only major cache
without promote/demote tooling; now all 5 levers (stage1,
stage2, stage3-v2, stake-scaling, gate-threshold) flow through
the same auditable atomic-swap + backup + lineage-stamp + audit-
row pipeline. Verdict gates on source-file existence +
lineage.built_at_utc freshness (no Brier history at the
Stage-1 cache layer). Wired into `promote.py status` and the
fast Wilson-UB demote check so the auto-daemon sees stage1
alongside the other 4 levers. Direct prereq for Active #8's
eventual ENFORCE flip of Alt A. Earlier same day:
Stage-1 Alt A runtime
shadow logging shipped: new `--stage1-shadow-empirical-override
{off,shadow}` CLI flag (default off) wires through
live_engine_cli -> live_engine.__init__ bridge ->
SignalEngine -> signal_pipeline_gates_post_fv. When `shadow`,
the post-FV phase computes `fair_value_alt_empirical = sigmoid(
logit(empirical) + s2_delta + s3_delta)` then runs it through
the same production calibrator and logs both prod + alt FVs
on every candidate row. NO decision change. Training table
pulls the 5 new alt fields through; offline shadow-override
report now PREFERS runtime-logged alt over its offline
fallback (with source breakdown surfaced). This is the
last code change before Active #8's eventual ENFORCE flip
(one config change). Earlier same day:
Stage-1 shadow-override
report shipped: replays two candidate Stage-1 fixes (Alt A
empirical-when-available, Alt B block fallback_level >= 2)
against actual training-table outcomes and surfaces the
counterfactual impact. **First production run shows Alt A
would reduce trailing-30d bias from +27.1pp to +21.2pp**
(6.0pp improvement on 32 of 87 bets = 37% coverage), with
the recommendation `promote_to_runtime_shadow` firing.
**Alt B blocks 6 bets, counterfactual delta = +$15** (3W/3L
blocked, just below the $20 recommendation threshold).
This is the shadow-first evidence Active #8 needs before
promoting either change to live FV math. Earlier same day:
Stage-1 cell-conditional
loss attribution shipped: drills Active #10's headline finding
(Stage-1 owns ~100% of the 27pp bias) into Stage-1-internal
cohort dimensions [fallback level, line fallback mode,
used_fallback, sample-size bucket, Poisson-vs-empirical gap].
First production run on 88 settled bets reveals (a)
**fallback_rate is 69%** -- we mostly bet in cells where the
runtime fell back to broader buckets, (b) **Poisson smoothing
inflates by +16pp vs the cell's own empirical rate** when both
are available -- the smoking gun for the Stage-1 over-prediction,
(c) `stage1_fallback_level_bucket=level_2plus_fallback` is the
worst cohort with +40pp bias (1.44x aggregate). This narrows
Active #8's retrain surface from "rebuild Stage-1 wholesale" to
specifically (i) tighten the fallback path and/or (ii) revise
the Poisson smoothing toward empirical-when-available. Earlier
same day: Active #16 v3 lineage
visibility shipped: startup-time per-artifact INFO log line on
every cache load (Stage-1, Stage-2, Stage-3 v2 weights,
calibrator) so the operator can grep the runtime log to see
"which version was live during this session"; new
`cache_lineage_freshness_health` block in daily review surfaces
each artifact's build-age + git_sha summary + fires a stale-cache
alert when build_age > 14d. First production run shows
calibrator artifacts (built earlier today via v1 stamping) have
proper lineage at 0.8d age; Stage-1/Stage-2 caches show
`no_lineage_pre_v2` (will get lineage on next refresh).
Earlier same day: Active #16 v2 lineage
extension shipped: build-time lineage now stamped on the
Stage-1 cache, Stage-2 cache, Stage-3 v2 weights, EV-policy
artifacts (report + 3 model JSONs), and the walk-forward
certification report -- closes the 5 "Defer to v2" follow-ups
from this morning's v1 shipment. Particularly timely: today's
loss attribution identified Stage-1 as owning the 27pp bias,
so the next time the Stage-1 cache is rebuilt (Active #8) the
new cache will carry full lineage from day one. Earlier same
day: Active #10 bet-level loss
attribution shipped: per-bet 4-stage probability decomposition
via the logit-additive FV chain, aggregated to surface "which
stage owns the bias." First production run on 87 filled+settled
bets reveals **Stage-1 owns ~100% of the 27pp over-prediction
bias** (mean_p0=92.7%, mean_won=65.5%). Stage-2 contributes
+0.04pp, Stage-3 actively *helps* by -0.05pp, calibration is 0
(shadow mode). This pinpoints Active #8's retrain target: the
Stage-1 Poisson cache, not the Stage-2/3 weights. Earlier same
day: Active #9 per-cohort
calibration drift detection shipped: 8th drift dimension.
Mirrors cohort_roi_health decomposition (edge / ask / inning /
line / current-state-edge bucket) on calibration: per-cohort
Brier + reliability gap vs aggregate. Two alert classes:
aggregate-level (whole model >= 10pp gap, fires regardless of
cohort breakdown) and per-cohort vs aggregate (>= 2x ratio with
n >= 30). First production run on 44 settled bets fired the
aggregate alert: **22.1pp reliability gap (mean_fv 92.6% vs
mean_won 70.5%) -- model is over-predicting Overs systematically**;
auto-attributed to 3 drifted inputs (stage2_run_env_delta PSI
2.32, base_fair_value 1.75, team_offense_delta 1.37).
Validates the zero-bet audit hypothesis: aggregate
calibration_health was scoring "calibrator picked OK method"
green while the calibrator's outputs themselves were 22pp off.
Earlier same day: Active #11 counterfactual
gate-change logger shipped: per-gate × per-alt-threshold × per-
time-window cross-tab; top_recommendations ranked by trailing-
30d realized-$ saved; daily-review block mirrors high-impact
tightenings to Notes. First production run on 178 settled bets
surfaced 7 actionable tightenings: gate_min_entry_ask 0.55->0.65
saves $75.64/30d, gate_max_base_fv 0.99->0.95 saves $54.12/30d
(high conf), gate_min_current_total 4->5 saves $44.31/30d.
Earlier same day: Active #16 model lineage
tracking shipped: both calibration artifacts + all four
promote.py audit rows now carry build-time + promotion-time
lineage [git_sha, builder_path, input_hashes]. Completes Phase
C v2 safety triangle: #12 detect / #13 react / #16 explain.
Earlier same day: Active #13 fast Wilson-UB demotion shipped:
parallel demote check fires in 5-6 days vs the 14d windowed
check; 95% one-sided confidence on Wilson UB < breakeven;
daemon bypasses standard cooldown for `fast_demote` actions.
Earlier same day:
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

- **UNDER outcomes counterfactual trailing-7d aggregate** *(2026-05-19)* --
  the natural smoothing layer on top of this afternoon's per-day
  UNDER outcomes block. The per-day window is too sparse for the
  n>=30 alert threshold to fire reliably (~30% of shadow_under
  candidates settle by daily-review time, so a single day rarely
  clears the floor). This ship walks the prior 7 dates' candidate
  + outcomes files, unions the settled rows, and re-aggregates --
  so the trailing alert at n>=50 starts firing within ~3 sessions
  of the operator opting into A5 emission instead of waiting for
  a single "lucky" day to hit n=30 alone.

  **Refactor**: extracted 3 helpers from the original block so
  per-day and trailing aggregates share the same arithmetic and
  cohort logic:
  - `_collect_under_settled_rows(session_date, candidate_dir,
    stake_usdc)`: loads shadow_under candidates + matches
    outcomes + computes won/profit per row. Returns
    `{settled_rows, n_shadow_under_candidates, n_missing_outcome,
    n_missing_ask, status, error?}`. Used by both per-day and the
    trailing loop.
  - `_aggregate_under_settled(settled_rows, stake_usdc)`: returns
    the aggregate dict (n, win_rate, total_counterfactual_pnl,
    counterfactual_roi, mean_under_ask, mean_under_fv) regardless
    of input window so per-day vs trailing can be compared
    directly.
  - `_under_settled_by_cohort(settled_rows, stake_usdc)`:
    5-dimensional cohort breakdown.

  **Trailing-7d sub-block** in
  `_under_outcomes_counterfactual_health`:
  - Walks today + prior 6 dates (`UNDER_OUTCOMES_TRAILING_DAYS`).
    For each date with a candidate file, runs the per-day
    collector and accumulates settled_rows into a single trailing
    list.
  - Surfaces: `anchor_date`, `trailing_days`, `dates_with_data` /
    `dates_missing` splits + counts, `n_shadow_under_candidates_total`,
    `n_settled_total`, `n_missing_outcome_total`, `n_missing_ask_total`,
    `aggregate` + `by_cohort` (same shape as per-day), `by_date`
    drill-down (sorted by date) with per-day n_settled / win_rate /
    pnl / roi, and `date_range` (earliest -> latest date with data).
  - 3-way status mirrors per-day: `ok` (has settled rows) /
    `no_settled` (shadow_under emitted but no outcomes) /
    `no_shadow_under_candidates` (A5 emission was off across the
    window) / `no_session_history` (no candidate files at all).
  - **2 sample-size-gated trailing alerts** (separate from per-day):
    n>=50 settled (vs per-day n>=30). Both prefixed with `(7d)` in
    the message text so the Notes block distinguishes window:
    - **Profitable** (trailing ROI >= +5%): "(7d) trailing-7d
      UNDER counterfactual X% ROI on N settled across M dates;
      Phase B4 paper-bet milestone progress: M/60 sessions
      accumulated."
    - **Unprofitable** (trailing ROI <= -5%): "(7d) trailing-7d
      UNDER signal is loss-making; aggregate is more stable than
      per-day view; tune UNDER-specific gates before any B4 flip."

  **Tolerates missing dates gracefully**: the operator's candidate
  log dir may not have all 7 prior dates (different paper-trading
  runs, gaps from non-trading days, etc.). Missing dates land in
  `dates_missing`; the trailing aggregate just runs over whatever
  data is present.

  **Cache-efficient**: today's date reuses the per-day collector's
  output rather than re-loading the candidate file twice.

  **First production run** (smoke against today's
  `candidate_universe`): per-day status =
  `no_shadow_under_candidates` (A5 emission flag shipped today;
  the operator hadn't opted in yet for yesterday's 2026-05-18
  session). Trailing-7d status = `no_shadow_under_candidates` too
  (1 of 7 trailing dates has any candidate file, and that one has
  0 shadow_under rows). 0 alerts. Once the operator runs
  `--under-emission-mode shadow` tomorrow + accumulates a few
  sessions, the trailing aggregate will surface a stable ROI
  signal -- and the B4 paper-bet milestone progress counter will
  start incrementing automatically.

  **Tests**: 10 new in `tests/test_build_daily_human_review_report.py`
  `UnderOutcomesCounterfactualHealthTests`:
  - trailing_7d sub-block always present
  - unions settled rows across dates (no double counting)
  - by_date breakdown sorted chronologically
  - date_range uses actual dates with data
  - profitable alert fires at n>=50 with +ROI
  - unprofitable alert fires at n>=50 with -ROI
  - no trailing alert when n<50 (sample-size gate)
  - status=`no_settled` when shadow_under exists but no outcomes
  - status=`no_shadow_under_candidates` when no UNDER emission
  - missing dates tolerated gracefully

  **1383 tests + 41 subtests pass** (+10 from this ship; existing
  14 per-day tests still pass unchanged after the helper extraction).

  **Files**: `scripts/analysis/build_daily_human_review_report.py`
  (added `timedelta` to datetime imports, 2 new constants
  `UNDER_OUTCOMES_TRAILING_DAYS` + `UNDER_OUTCOMES_TRAILING_MIN_N_FOR_ALERT`,
  3 extracted helpers, refactored block body + trailing-7d loop
  + trailing alerts; net +~210 LOC),
  `tests/test_build_daily_human_review_report.py` (+10 tests).

- **Phase A5 follow-up #2: UNDER outcomes counterfactual block** *(2026-05-19)* --
  the natural completion of the A5 -> UNDER coverage trilogy.
  A5 ships UNDER candidate emission; the UNDER coverage block
  surfaces decision distribution; this block answers the
  operationally critical question "would the bot have made or
  lost money trading UNDER?" -- the data point Phase B4
  (60-session UNDER paper-bet validation milestone) ultimately
  depends on.

  **Block** (`scripts/analysis/build_daily_human_review_report.py`):
  - New `_under_outcomes_counterfactual_health(session_date,
    candidate_dir, stake_usdc)` reads today's `_candidates.jsonl`
    + `_outcomes.jsonl` (existing artifacts; no new I/O needed).
  - Filters `_candidates.jsonl` to `(side="under" AND
    decision="shadow_under")` -- only the UNDER candidates that
    cleared their min_edge gate.
  - Builds `(game_pk, line_str) -> final_total` map from
    `_outcomes.jsonl` (covers every game with a candidate -- one
    row per (game_pk, line) regardless of side).
  - Per shadow_under candidate, settles: UNDER wins iff
    `final_total < line` (strictly less; MLB OU lines end in .5
    so no pushes). Counterfactual P&L mirrors paper-mode OVER
    taker math: `stake / entry_ask` if won, `-stake` if lost.
    Stake configurable; defaults to $10.
  - Aggregate metrics: `n_settled`, `n_won`, `n_lost`,
    `win_rate`, `total_counterfactual_pnl`,
    `total_counterfactual_stake`, `counterfactual_roi`,
    `mean_under_ask`, `mean_under_fv`.
  - 5-dimensional per-cohort breakdown (edge / inning / line /
    ask / current_state_edge) consistent with the rest of the
    daily review.
  - Tracks `n_missing_outcome` (game settlement not yet in
    outcomes.jsonl -- rerun later) + `n_missing_ask` (UNDER ask
    None or out of (0,1) -- skipped from the counterfactual).

  **Statuses**:
  - `no_shadow_under_candidates`: 0 rows. Operator didn't opt
    into A5 emission OR no candidate cleared UNDER min_edge.
    No alert.
  - `no_settled`: shadow_under rows exist but 0 have outcomes
    (outcomes.jsonl not written yet). Surface; no alert.
  - `ok`: have at least 1 settled outcome; alerts apply.
  - `check_error`: file read errors. Diagnostic only.

  **2 sample-size-gated alerts** (only when `status=ok`):
  - **Profitable**: `n_settled >= 30` AND `counterfactual_roi >=
    +5%` -> "UNDER candidates would have netted +X% ROI;
    consider Phase B4 paper-bet milestone if durable."
  - **Unprofitable**: `n_settled >= 30` AND `counterfactual_roi
    <= -5%` -> "UNDER signal is loss-making; tune UNDER-specific
    gates BEFORE any B4 flip."

  Both mirror via Notes prefix `Under-outcomes:`.

  **Fail-open**: any helper exception sets `status=check_error`;
  daily review never blocks on this block.

  **A5 wart fixed in the same patch** (`signal_pipeline.py
  ::_maybe_emit_under_candidate`): `decision_ask` was inheriting
  OVER's value via the dict copy because only `entry_ask` /
  `decision_best_ask` were being overwritten. Cohort-by-ask
  aggregation downstream would have bucketed UNDER candidates
  by OVER's ask. Fixed by adding the explicit overwrite. Caught
  during this block's design.

  **First production run against yesterday's 2026-05-18 session**:
  status=`no_shadow_under_candidates`, 0 alerts. Correct baseline
  -- the A5 emission flag shipped today, so yesterday's session
  predates it. Tomorrow's run (after the operator passes
  `--under-emission-mode shadow`) will surface real
  counterfactual P&L.

  **What this unlocks**:
  - Operator can read daily-review `Under-outcomes:` alerts to
    monitor whether UNDER signals are profitable across the 7-day
    paper-mode runway. If consistently positive at meaningful
    sample, the Phase B4 paper-bet milestone is a config flip
    away.
  - Per-cohort table answers "WHICH UNDER candidates would have
    been profitable" -- enables UNDER-specific gate tuning when
    cohort sample grows (e.g., raise UNDER min_edge in cohorts
    where Alt A is losing).
  - Closes the A5 observability trilogy: emission (A5) ->
    decision distribution (UNDER coverage) -> outcomes
    counterfactual (this block).

  **Tests**: 14 new in `tests/test_build_daily_human_review_report.py`
  `UnderOutcomesCounterfactualHealthTests`:
  - missing candidate file -> `check_error`
  - 0 shadow_under rows -> `no_shadow_under_candidates`, no alerts
  - shadow_under rows but no outcomes -> `no_settled`
  - UNDER wins when `final_total < line` (math verified)
  - UNDER loses when `final_total >= line` (math verified)
  - profitable alert at >=5% ROI + >=30 settled
  - unprofitable alert at <=-5% ROI + >=30 settled
  - no alert at n<30 (sample-size gate)
  - no alert at near-breakeven ROI (|roi| < 5%)
  - per-cohort breakdown partitions correctly
  - invalid ask values tracked in `n_missing_ask`
  - missing outcome rows tracked in `n_missing_outcome`
  - skip-decision rows (gate_min_edge / gate_no_under_liquidity)
    excluded from shadow_under set
  - Notes prefix is `Under-outcomes:`

  **1373 tests + 41 subtests pass** (+14 from this ship).

  **Files**: `scripts/analysis/build_daily_human_review_report.py`
  (+~240 LOC: 4 new constants, new
  `_under_outcomes_counterfactual_health` block + 2 sample-
  size-gated alerts, `build_report` + `_build_notes` + return-
  dict wiring), `scripts/trading/signal_pipeline.py` (+4 LOC:
  fix `decision_ask` overwrite in `_maybe_emit_under_candidate`),
  `tests/test_build_daily_human_review_report.py` (+14 tests).

- **Shadow-override report cohort breakdown** *(2026-05-19)* --
  the natural next consumer of today's morning fixes (schema
  propagation + refresh `--mode both`) that landed Alt-A
  diagnostics into the training table. Before this ship, the
  shadow-override report presented a single aggregate number
  ("Alt A reduces 30d bias by 6pp"); operators couldn't tell
  WHERE Alt-A helps or whether it makes any cohort worse. This
  ship surfaces 5-dimensional cohort cuts so the eventual
  Active #8 ENFORCE flip can be SCOPED to specific cohorts
  rather than all-or-nothing global.

  **Schema extension** (`ShadowBet`):
  - 3 new fields: `edge_at_ask`, `decision_ask`,
    `current_state_value_edge` (with forward-compat fallback to
    `edge` / `entry_ask` aliases for older training-table rows).

  **5 cohort bucketers** (mirror `cohort_roi_health` /
  `cohort_calibration_health` so cross-block comparison stays
  consistent):
  - `_edge_bucket`: <0.15 / 0.15-0.18 / 0.18-0.22 / >=0.22
  - `_inning_bucket`: <=5 / 6 / 7 / >=8
  - `_line_bucket`: <=7.5 / 8.5 / 9.5 / >=10.5
  - `_ask_bucket`: <0.55 / 0.55-0.70 / 0.70-0.85 / >=0.85
  - `_current_state_edge_bucket`: <0.00 / 0.00-0.03 / 0.03-0.06
    / >=0.06

  **Per-cohort aggregator** (`_aggregate_cohort`): production
  vs Alt A bias delta + Alt B kept/blocked split. Slimmer than
  `aggregate_window` because cohorts are sliced thinly --
  source breakdown + recommendation subtree intentionally
  excluded to keep the JSON readable.

  **Top cohorts summary** (`build_cohort_breakdown` ->
  `top_cohorts`):
  - `most_improved`: cohorts ranked by `bias_delta_vs_prod_pp`
    descending (where Alt A helps most)
  - `regressions`: cohorts with negative `bias_delta` (where
    Alt A makes bias WORSE)
  - `highest_coverage`: cohorts where Alt A applies to most bets
  - `largest_alt_b_savings`: cohorts with highest Alt B
    counterfactual P&L savings (positive only in markdown view)
  - All entries gated on `min_n_per_cohort=5` (5 chosen as the
    floor where `mean(won)` starts to mean anything; below that
    the bias delta is dominated by noise).

  **Markdown render** (`_cohort_md`): per-dimension tables
  (`bucket | n | prod_bias | alt_a_bias | delta_pp | coverage |
  alt_b_blocked | alt_b_$`) plus the 4 top-cohort summary
  sections. Section only renders when `n_bets_total > 0` so
  empty windows don't pollute the output.

  **Daily-review surface**
  (`build_daily_human_review_report.py::_stage1_shadow_override_health`):
  - New `cohort_breakdown_30d` block in the return dict
    carrying compact (top-3) views of most_improved /
    regressions / highest_coverage / largest_alt_b_savings.
  - 2 new sample-size-gated alerts:
    * **Scoped-promotion suggestion**: when the best cohort's
      `bias_delta >= 1pp`, surfaces a Notes line "consider a
      scoped promotion on this cohort before flipping Alt A
      globally". Helps the operator see cohorts where ENFORCE
      would clearly help even when the aggregate alert hasn't
      fired (or has).
    * **Regression warning**: when the worst cohort's
      `bias_delta <= -2pp`, surfaces "REGRESSES under Alt A;
      a global promote would hurt this cohort". The -2pp
      threshold is stricter than improvements' +1pp because
      regressions can hide inside an aggregate-positive Alt A
      average, and acting on one would lock in damage.

  **First live production run** (trailing 30d, 97 bets):
  - **Top improvements**:
    * `current_state_edge_bucket=<0.00` (n=19): +13.30pp delta,
      53% coverage. Negative current-state-edge bets are where
      Alt A helps MOST.
    * `ask_bucket=0.55-0.70` (n=30): +8.94pp delta, 37%
      coverage. Mid-ask range.
    * `line_bucket=>=10.5` (n=16): +8.34pp delta, 50% coverage.
      Highest-scoring lines.
  - **Critical regression**: `inning_bucket=>=8` (n=7) sees
    Alt A REGRESS by **-23.83pp** -- late innings are where Alt
    A makes bias WORSE. A global Alt A ENFORCE flip would
    significantly hurt this cohort. The scoped-promotion
    pattern (enforce on cohorts where it helps + exclude
    inning>=8) is now the operationally correct path.
  - **Alt B savings concentrated on**: `line_bucket=>=10.5`
    saves $14.29 (3 blocked), `edge_bucket=0.15-0.18` saves
    $12.29 (3 blocked), `inning_bucket=6` saves $10.28 (5
    blocked).
  - 3 Notes alerts fire on the first run: existing aggregate
    Alt A recommendation + NEW scoped-promotion suggestion for
    the negative-current-state-edge cohort + NEW regression
    warning for inning>=8.

  **What this unlocks**: instead of an all-or-nothing
  ENFORCE flip, the operator can now design a runtime-overrides
  config that applies Alt A conditionally (e.g.,
  `enforce_alt_a_when: current_state_value_edge < 0` AND
  `inning < 8`). Reuses the existing
  `cache/live_engine_overrides.json` lever shipped earlier;
  no new infrastructure needed. The eventual Active #8
  decision becomes: SCOPED ENFORCE on cohorts where Alt A
  durably helps, leave production untouched where it doesn't.

  **What v2 should add** (deferred; not blocking):
  - **Cross-dimension cohort cuts** (e.g. edge>=0.22 AND
    inning<8 simultaneously). Today the cohort cuts are
    per-dimension only; cross-cuts would surface "Alt A helps
    on high-edge late-game ONLY when current_state_edge is
    negative." Useful but exponentially noisier; defer until
    more sample.
  - **Cohort-conditional recommendation engine**: today's
    daily-review block surfaces the TOP cohort; a future
    version could emit a structured recommendation row per
    cohort that clears its own threshold, so the auto-daemon
    can act on multi-cohort verdicts.

  **Tests**: 16 new in
  `tests/test_build_stage1_shadow_override_report.py`
  `CohortBreakdownTests` + 5 new in
  `tests/test_build_daily_human_review_report.py`
  `Stage1ShadowOverrideHealthTests`:
  - All 5 bucketers' ranges + missing-value handling
  - `project_bet` carries the new cohort fields + falls back
    to alias columns
  - `_aggregate_cohort` empty case + bias delta computation
  - `build_cohort_breakdown` groups by each dimension
  - `top_cohorts` most_improved sorted descending
  - `top_cohorts` regressions only negative deltas
  - `top_cohorts` excludes cohorts below `min_n_per_cohort`
  - `largest_alt_b_savings` includes positive entries
  - Payload includes `cohort_breakdown_trailing_30d`
  - Markdown renders the cohort section when present
  - Daily-review block surfaces `cohort_breakdown_30d`
  - Scoped-promotion alert fires on cohort >= 1pp improvement
  - Small-improvement cohort does NOT fire scoped-promotion alert
  - Regression alert fires on cohort <= -2pp
  - Small regression does NOT fire regression alert

  **1359 tests + 41 subtests pass** (+21 from this ship).

  **Files**: `scripts/analysis/build_stage1_shadow_override_report.py`
  (+~280 LOC: 3 new ShadowBet fields, 5 cohort bucketers,
  `_aggregate_cohort` + `build_cohort_breakdown` +
  `_cohort_md`, `COHORT_DIMENSIONS` constant,
  `COHORT_MIN_N_FOR_AGGREGATE` constant; `build_payload` +
  `render_markdown` extended),
  `scripts/analysis/build_daily_human_review_report.py`
  (+~70 LOC: extend `_stage1_shadow_override_health` with
  cohort_breakdown_30d block + 2 new alert classes),
  `tests/test_build_stage1_shadow_override_report.py` (+16
  tests), `tests/test_build_daily_human_review_report.py` (+5
  tests).

- **Phase A5 follow-up: UNDER emission daily-review block** *(2026-05-19)* --
  closes the observability loop on this morning's A5 ship. The
  A5 helper emits sibling UNDER candidate rows when
  `--under-emission-mode shadow`, but without a daily-review
  surface the operator can't see whether emission is working or
  whether the borrowed OVER thresholds are sensible on UNDER's
  price dynamics. This block answers both questions in the daily
  human-review JSON + markdown automatically.

  **Block** (`scripts/analysis/build_daily_human_review_report.py`):
  - New `_under_emission_health(session_date, candidate_dir)`
    reads the per-date candidate log and surfaces:
    * Coverage: `over_post_fv_count`, `under_emitted_count`,
      `coverage_rate` (UNDER emitted / OVER FV-phase ticks; target
      ~100% when mode=shadow with healthy UNDER liquidity).
    * Decision breakdown: `n_shadow_under` (would-have-traded),
      `n_gate_min_edge`, `n_gate_no_under_liquidity`, plus
      `shadow_under_rate` and `liquidity_skip_rate`.
    * Price quality: `mean_under_fv`, `mean_under_fv_raw`,
      `mean_under_ask`, `mean_under_edge`,
      `mean_under_calibration_delta`, FV histogram across 5
      asymmetric buckets (0.00-0.20 / 0.20-0.40 / 0.40-0.60 /
      0.60-0.80 / 0.80-1.00).
    * `under_pair_available_rate`: pair availability surfaced
      from the per-row flag (the existing aggregate version is
      session-level; this is conditioned on the UNDER candidate
      being emitted).

  **3-way status** (matches the operational reality of A5 rollout):
  - `not_emitting`: 0 UNDER rows. Operator did not pass
    `--under-emission-mode shadow`. No alert.
  - `no_liquidity`: UNDER rows exist but 100% are
    `gate_no_under_liquidity` skips. Mode active but UNDER book
    is empty across the session. Surface but no alert (could be
    a market-wide issue, not actionable).
  - `ok`: UNDER emission is producing decisions; sample-size-
    gated alerts apply.

  **3 sample-size-gated alert classes** (only fire when `status=ok`):
  - **Coverage gap**: `coverage_rate < 50%` AND
    `under_emitted >= 50` -> alert. Means more than half of OVER
    FV-phase ticks have no UNDER candidate row; investigate
    `_maybe_emit_under_candidate` skips or thin book liquidity.
  - **Suspiciously loose UNDER gates**: `shadow_under_rate > 50%`
    AND `n_under >= 20` -> alert. Either UNDER has genuine edge
    OR the borrowed OVER `edge_threshold` is too loose for
    UNDER's price dynamics. Human-read prompt.
  - **Suspiciously tight UNDER gates**: `shadow_under_rate < 2%`
    AND `n_under >= 100` -> alert. Borrowed OVER edge_threshold
    likely too tight; tune UNDER-specific `min_edge` from the
    accumulated shadow data.

  All three mirror via Notes prefix `Under-coverage:`.

  **Wired into `build_report`** with the same pattern as
  `_promotion_lag_health` and other recent blocks (return-dict
  key + `_build_notes` signature). Backward-compatible: the
  parameter is `Optional[Dict[str, Any]] = None` so existing
  callers (tests, ad-hoc scripts) don't break.

  **Fail-open**: any helper exception sets `status=check_error`;
  daily review never blocks on this block.

  **First production run against yesterday's 2026-05-18 paper
  session**: status=`not_emitting`, `over_post_fv_count=702`,
  `under_emitted_count=0`, 0 alerts. This is the correct
  baseline -- the A5 emission flag shipped today, so the operator
  hadn't yet opted in for yesterday's session. Tomorrow's run
  (after the operator passes `--under-emission-mode shadow`)
  will surface real UNDER decision distribution.

  **Tests**: 13 new in `tests/test_build_daily_human_review_report.py`
  `UnderEmissionHealthTests`:
  - missing candidate file -> `check_error`
  - 0 UNDER rows -> `not_emitting`, no alerts
  - 100% `gate_no_under_liquidity` -> `no_liquidity`, no alerts
  - ok status: decision breakdown + coverage_rate computed
  - coverage alert fires when rate < 50% AND n >= 50
  - coverage alert SUPPRESSED when n < 50 (sample-size gate)
  - shadow_under high alert fires when rate > 50% AND n >= 20
  - shadow_under low alert fires when rate < 2% AND n >= 100
  - shadow_under low SUPPRESSED when n < 100
  - price-quality aggregates (mean_fv, mean_ask, mean_edge,
    calibration delta) compute correctly
  - FV bucket distribution counts each bucket correctly
  - pair-available rate propagates
  - Notes prefix is `Under-coverage:`

  **1338 tests + 41 subtests pass** (+13 from this ship).

  **Files**: `scripts/analysis/build_daily_human_review_report.py`
  (+~210 LOC: 9 new constants, new `_under_emission_health` block,
  `build_report` + `_build_notes` + return-dict wiring); 13 new
  tests in `tests/test_build_daily_human_review_report.py`.

- **Phase A5: live UNDER candidate emission (shadow mode)** *(2026-05-19)* --
  the keystone for the bidirectional pivot. Before today, UNDER
  analysis ran purely offline as the OVER candidate's synthesized
  complement, carrying selection bias from OVER's gate funnel.
  Now the live engine emits a sibling UNDER candidate row alongside
  every OVER candidate that reaches the FV phase, with its own
  calibrated FV, its own UNDER-side market data, and its own gate
  evaluation -- writing to the same `_candidates.jsonl` log via the
  standard candidate-decision path so the daily-review by_side
  block, training table, loss-attribution, and shadow-override
  reports all pick UNDER up automatically.

  **CLI flag** (`scripts/trading/live_engine_cli.py` +
  `scripts/trading/signal_config.py`):
  - `--under-emission-mode {off, shadow}` (default `off`). `off`
    preserves existing OVER-only behavior. `shadow` emits UNDER
    candidate rows; NO UNDER bets are placed in either mode
    (paper or live). Eventual UNDER paper-bet flip is a separate
    ship gated by B4 60-session validation.
  - `--prob-calibration-under-path PATH` (default
    `data/analysis_output/calibration/signal_win_calibration_under.json`)
    points at the separately-trained UNDER calibrator artifact.

  **Engine startup** (`scripts/trading/signal_engine.py`):
  - Reads `trade_args.under_emission_mode`. When `shadow`, loads the
    UNDER `ProbabilityCalibrator` via `from_path(...)`, logs
    method + path, stamps lineage via the existing
    `_log_artifact_lineage_summary("calibrator_under", ...)` hook.
  - Fail-open: a missing/unreadable UNDER calibrator logs a
    warning and falls back to identity calibration
    (`under_fv = 1 - over_fv_raw` uncalibrated) -- emission still
    happens so the operator sees the gap.

  **Helper** (`scripts/trading/signal_pipeline.py`):
  - New `_maybe_emit_under_candidate(engine, ctx, over_fv_phase,
    over_candidate_payload)`. No-op when mode != shadow. When
    active:
    * Computes `under_fv_raw = 1 - over_fv_raw`
    * Calibrates through the UNDER calibrator (identity when
      none loaded)
    * Reads `under_best_ask` / `under_best_bid` /
      `under_pair_available` from the tick book
    * Builds an UNDER candidate payload by COPYING the OVER row
      (preserves state-value / weather / Stage-1 support
      metadata) and overwriting side-specific fields:
      `side="under"`, `bet_id="<over>_under_shadow"`,
      `over_bet_id=<over>`, `entry_ask=under_ask`,
      `fair_value=under_fv`, `fair_value_raw=under_fv_raw`,
      `base_fair_value=1 - over_base`,
      `stage2_run_env_delta=-over_s2`,
      `team_offense_delta=-over_s3` (logit-additive deltas on
      OVER => equivalent magnitude opposite sign on UNDER)
    * Applies a minimal gate (`min_edge` using OVER's
      `edge_threshold` for the MVP; UNDER-specific thresholds
      get tuned from the shadow data as it accumulates)
    * Decision = `shadow_under` if UNDER gates pass, else
      `skip` with reason (`gate_min_edge` or
      `gate_no_under_liquidity` when UNDER ask is None)
    * Writes via `engine._record_candidate_decision(payload)` so
      the row lands in the same `_candidates.jsonl` as OVER
  - Hooked into `process_tick` right after
    `_maybe_emit_shadow_quote` (line 394) and BEFORE OVER's
    late-stage gates run, so UNDER coverage is not correlated
    with OVER's post-FV gate filtering.
  - Fail-open throughout: any helper exception logs at DEBUG
    and continues; OVER's pipeline is unaffected.

  **`live_engine.py` bridge**: `trade_args.under_emission_mode =
  getattr(live_args, "under_emission_mode", "off")` before
  `super().__init__(...)`. Mirrors the existing
  `stage1_shadow_empirical_mode` bridge pattern.

  **What this unlocks**:
  - The 7-day paper-mode runway now collects UNDER candidate data
    in addition to OVER. At the bot's typical ~600 OVER candidates
    per session, that's ~600 UNDER rows per day too -- enough to
    populate the daily review's `by_side.under` subtotals
    meaningfully within a week.
  - Phase B1 side-aware drift alerts (already shipped for
    `calibration_health`) can now compare OVER vs UNDER signal
    quality on like-with-like data instead of OVER's complement
    bias.
  - Future B4 paper-mode UNDER-bet validation (the 60-session
    milestone before any UNDER live trading) becomes a config
    flip -- the data pipeline already exists.
  - Phase C two-sided quoting (`--quote-engine-mode` shadow,
    shipped 2026-05-17) already reads `under_best_ask` from the
    book; with this ship, the quote engine's UNDER-side
    reasoning is now backed by a calibrated UNDER FV from the
    same pipeline that produces OVER FV.

  **What this MVP intentionally does not do** (deferred):
  - **No UNDER paper bets**: pure logging only. Adds UNDER rows
    with `decision=shadow_under` (when gates pass) or `skip`
    (when gates fail); never calls `_place_bet`. The eventual
    paper-bet flip is gated by enough shadow data to characterize
    UNDER calibration reliability + win rate distribution.
  - **No UNDER-specific gate thresholds**: rides OVER's
    `edge_threshold` for UNDER `min_edge` for now. UNDER market
    dynamics differ (typically high asks when UNDER is favored);
    tune from data once enough shadow rows accumulate.
  - **No UNDER-side post-FV gates** (extreme_edge, FV/ask gap,
    inning floors): MVP just applies `min_edge`. The full UNDER
    gate stack lands when B4 paper-bet validation starts.

  **Tests**: 12 new in `tests/test_under_candidate_emission.py`:
  - mode='off' is a no-op
  - mode='shadow' emits row with side='under' + correct bet_id
  - UNDER calibrator called with `(1 - over_fv_raw)`
  - missing UNDER ask -> `gate_no_under_liquidity` skip
  - UNDER edge above threshold -> `shadow_under` decision
  - UNDER edge below threshold -> `gate_min_edge` skip
  - Stage-2 + Stage-3 deltas propagate with inverted sign
  - `under_pair_available` propagates
  - Helper is fail-open on internal exceptions
  - No-op when `over_fv_raw` is None (undefined complement)
  - No-calibrator falls back to identity complement
  - live_engine.py bridge source-string check

  **Smoke test against the live import graph**: CLI flag parses
  cleanly through both `signal_config.parse_trade_args` and
  `live_engine_cli.parse_live_args`; helper produces UNDER row
  with correct side, bet_id suffix, calibrated FV, inverted
  Stage-2/3 deltas, `decision=shadow_under`, `gate=passes`. End-
  to-end wiring confirmed.

  **1325 tests + 41 subtests pass** (+13 new tests this ship).

  **Files**: `scripts/trading/signal_config.py` (+~50 LOC: new
  `DEFAULT_PROB_CALIBRATION_UNDER_PATH` /
  `DEFAULT_UNDER_EMISSION_MODE` / `UNDER_EMISSION_MODES`
  constants; new `--under-emission-mode` +
  `--prob-calibration-under-path` argparse entries),
  `scripts/trading/signal_engine.py` (+~45 LOC: load UNDER
  calibrator + lineage stamp + fail-open warning),
  `scripts/trading/signal_pipeline.py` (+~120 LOC: new
  `_maybe_emit_under_candidate` helper + wire site in
  `process_tick`), `scripts/trading/live_engine_cli.py` (+~18
  LOC: new CLI flag),
  `scripts/trading/live_engine.py` (+~4 LOC: trade_args bridge),
  new `tests/test_under_candidate_emission.py` (12 tests).

- **Refresh `--mode live` hardcode fix (paper-mode propagation)**
  *(2026-05-19)* -- closes the wiring defect discovered during
  yesterday's paper-trading audit. The user is in paper-mode for a
  week explicitly to validate Alt-A, but `run_daily_refresh.py`
  hardcoded `--mode live` on the `unified_signals` and
  `signal_training_table` steps -- so paper bets never reached
  loss-attribution, shadow-override, training table, or any other
  downstream analysis. The paper-mode runway's whole purpose
  (Alt-A validation evidence) was being silently starved.

  **Fix**: both steps changed to `--mode both`. Safe because the
  consumers that matter (loss-attribution, shadow-override,
  cohort-calibration) use the `won` boolean (counterfactual:
  did over/under hit), which is identical for paper and live
  bets and independent of paper's 100% taker assumption. Steps
  that DO use realized P&L or fill behavior (`clv_report`,
  `execution_diagnostics`, `ev_policy_backtest`,
  `queue_aware_execution_replay`) stay `--mode live`
  intentionally.

  **Verified end-to-end after re-running both steps**: training
  table grew from 182 -> 247 rows (+65 paper rows from
  accumulated sessions); **12 rows now carry Alt-A diagnostics
  populated** (the 12 placed bets from yesterday's 2026-05-18
  paper session, all under `mode=paper`). Combined with this
  morning's earlier `unified_signal_table` schema fix that added
  the 6 Alt-A field extractors, the candidate -> training-table
  Alt-A propagation pipeline is now fully closed.

  **Tests**: 1 new in `tests/test_run_daily_refresh.py`:
  asserts unified_signals + signal_training_table run
  `--mode both` AND asserts fill-aware steps (clv_report,
  execution_diagnostics) stay `--mode live` -- guards against
  an over-eager future refactor that batch-converts everything.

  **Files**: `scripts/analysis/run_daily_refresh.py` (2 changed
  steps + descriptive comments explaining why each direction
  is correct).

- **Active #15: promotion-lag tracker + operator doc** *(2026-05-19)* --
  closes the last open Hygiene item. Every promote.py file-swap
  mutates a cache (or the runtime-overrides JSON) immediately, but
  the live engine doesn't pick up the new file until it boots its
  next session. Operators have asked "is my promote in effect yet?"
  enough times to deserve a structured answer in the daily review.

  **Daily-review block**
  (`scripts/analysis/build_daily_human_review_report.py`):
  - New `_promotion_lag_health` block returns per-lever verdicts for
    the 5 promote.py levers (stage1, stage2, stage3_v2,
    stake_scaling, gate_threshold).
  - Compares each lever's cache mtime against the most recent
    engine-boot timestamp, proxied by the first bet's `placed_at`
    from the latest session file across BOTH `data/live_trading/
    sessions/` and `data/paper_trading/sessions/` (so an operator
    in paper-mode -- today's scenario -- still gets accurate
    answers).
  - 5 per-lever statuses: `effective_in_runtime` (cache mtime <=
    boot, engine already loaded this version), `pending_next_
    session_boot` (cache newer than boot, restart will pick it up),
    `cache_missing` (lever never promoted -- first-time state),
    `no_session_history` (fresh install, no boot proxy),
    `check_error` (filesystem error, diagnostic only).
  - Alert when a lever is `pending_next_session_boot` AND lag
    exceeds 24h (operator promoted but forgot to restart). Mirrors
    via Notes prefix `Promotion-lag:`.

  **Helper functions**:
  - `_parse_iso_to_epoch_safe(value)`: fail-open ISO->epoch parser
    that returns None on bad input rather than raising. Used to
    compare session timestamps against file mtimes.
  - `_latest_session_start_utc(project_root, session_roots)`: walks
    both trading roots, returns `(filename, epoch, iso)` for the
    latest session's first bet `placed_at`. Falls back to session
    `generated_at` for sessions that placed zero bets.

  **shared-file lever handling**: `stake_scaling` and
  `gate_threshold` both mutate `cache/live_engine_overrides.json`.
  Any promote of either bumps the same mtime, so both levers report
  the same status; we surface them separately so an alert filed
  under the lever name you actually promoted is easy to grep for.

  **Operator doc** (`docs/operational/promotion_lag.md`): one-page
  reference explaining promote-time vs effect-time for each lever,
  how the tracker decides, how to clear a pending status, what the
  tracker doesn't catch (engine cache-loading bugs, hot-reload
  semantics), and pointers to related health blocks
  (`cache_lineage_freshness_health`,
  `cross_artifact_consistency_health`,
  `stage1_alt_a_staging_health`).

  **First production run on 2026-05-19**: last engine boot detected
  at 2026-05-19T00:07:38Z (first bet of the 2026-05-18 paper
  session). All 5 lever verdicts correct:
  - stage1: `pending_next_session_boot` (today's refresh rebuilt
    cache 0.32h ago; lag well under 24h, no alert)
  - stage2: `effective_in_runtime` (cache from 2026-05-09, picked
    up 239.5h before the boot)
  - stage3_v2, stake_scaling, gate_threshold: `cache_missing` (no
    operator has promoted these yet -- expected first-time state)
  - 0 alerts.

  **Files**: `scripts/analysis/build_daily_human_review_report.py`
  (+~180 LOC: new `PROMOTION_LAG_LEVERS` /
  `PROMOTION_LAG_SESSION_ROOTS` /
  `PROMOTION_LAG_PENDING_HOURS_WARN` constants;
  `_parse_iso_to_epoch_safe` +
  `_latest_session_start_utc` helpers; new `_promotion_lag_health`
  block; `build_report` + `_build_notes` + return-dict wiring); new
  `docs/operational/promotion_lag.md`. 12 new tests in
  `test_build_daily_human_review_report.py` covering both helpers +
  all 5 status outcomes + the alert threshold + the shared-file
  case + the Notes prefix. **1312 tests + 41 subtests pass.**

  **Hygiene section status**: with this ship, all three originally-
  listed Hygiene items are closed (#14 backup retention + PSI-history
  GC, #15 promotion-lag tracker, #16 model lineage tracking through
  v4 cross-artifact consistency). Future Hygiene additions only.

- **Unified-signal-table Alt-A propagation fix** *(2026-05-19)* --
  closes a wiring defect discovered during the 2026-05-18 paper-
  trading audit. The 2026-05-17 Stage-1 Alt-A runtime shadow ship
  populated 6 new fields on every score-event-family candidate row
  (`stage1_shadow_empirical_mode`, `fair_value_alt_empirical`, etc.)
  but the intermediate unified-signal-table builder
  (`scripts/analysis/unified_signal_table/`) uses an explicit
  allow-list pattern for both `SCHEMA_COLS` and the `build_row`
  field extractor. The 6 fields fell through the gap: the
  downstream training table declared the columns (added 2026-05-17)
  but populated 0 rows, breaking loss-attribution + shadow-override
  reports' ability to see runtime Alt-A on filled bets.

  **Fix** (small, mechanical):
  - Added 6 fields to `unified_signal_table/schema.py::SCHEMA_COLS`
    immediately after the `INFERENCE_PANEL_COLUMNS` block.
  - Added `_build_alt_a_shadow_fields(...)` helper in
    `unified_signal_table/row_builder.py` and wired it into the
    `build_row` call site so the extractor runs alongside the
    existing `_build_inferred_state_fields`, etc.

  **Tests**: 2 new tests in
  `tests/test_build_unified_signal_table.py`:
  - `test_build_master_rows_propagates_stage1_alt_a_shadow_fields`:
    feeds a candidate row carrying all 6 Alt-A fields, asserts they
    land on the master row + appear in `master_columns` (so the CSV
    writer doesn't drop them).
  - `test_build_master_rows_alt_a_fields_default_to_none_when_off`:
    pre-Alt-A candidate row (no shadow fields) propagates as
    None/False without raising.

  **What this enables**: once the next refresh folds in paper bets
  carrying Alt-A (currently blocked by the refresh's
  `--mode live` hardcode at `run_daily_refresh.py:1929` -- a
  separate follow-up), loss-attribution + shadow-override reports
  can start reading **runtime-decided** Alt-A instead of
  recomputing it offline. The 7-day paper-mode runway will
  generate the per-bet evidence Active #8's ENFORCE flip needs.

  **Files**: `scripts/analysis/unified_signal_table/schema.py`
  (+13 LOC: 6 field names + leading comment),
  `scripts/analysis/unified_signal_table/row_builder.py` (+72 LOC:
  new `_build_alt_a_shadow_fields` helper + call site).

- **Active #8 prep: Stage-1 Alt-A staging cache builder + refresh +
  daily-review surface** *(2026-05-18)* -- the natural consumer of
  yesterday's full Alt-A scaffolding (loss attribution → cell drill
  → shadow-override report → runtime shadow logging → promote.py
  stage1 subcommand). Before today, every piece existed EXCEPT a
  real on-disk Alt-A cache file for `promote.py stage1` to swap into
  production. The runtime's on-the-fly Alt-A shadow proved Alt A
  reduces 30d aggregate bias by 6pp (27.1pp -> 21.2pp on 37%
  coverage) but had no materialized artifact behind it. This ships
  the artifact + the refresh that keeps it fresh + the daily-review
  visibility so the operator can see it.

  **Builder change** (`cache/build_mlb_ou_cache.py`):
  - New `--smoothing-mode {poisson, empirical_when_available}` flag
    (default `poisson` -- production behavior unchanged).
  - New `--min-empirical-n-for-override INT` (default 0 to match the
    runtime shadow path's "always override when empirical present"
    behavior; raise the threshold to gate on per-cell sample size).
  - When `empirical_when_available`, new `_apply_alt_a_smoothing`
    helper walks every cell post-build and overwrites the `poXX`
    field with the same cell's `oXX` value whenever the empirical is
    a valid (0,1) probability AND `n_samples >= threshold`. Cells
    where empirical is at the 0/1 boundary (would blow up the
    logit-additive FV math) keep the Poisson smoothing.
  - New `alt_a_smoothing` block in cache meta records: `enabled`,
    `mode`, `min_empirical_n_for_override`, `cells_total`,
    `cells_overridden`, `cells_kept_poisson_low_n`,
    `cells_kept_poisson_no_empirical`,
    `cells_kept_poisson_invalid_empirical`, per-line override counts,
    `mean_abs_delta_logit`, `mean_signed_delta`, `n_line_deltas`.
    Diagnostic only; the runtime doesn't read it.
  - Lineage `cli_args_summary` extended with `smoothing_mode` +
    `min_empirical_n_for_override` so the existing
    `cross_artifact_consistency_health` block can distinguish Alt-A
    builds from poisson builds.

  **Refresh integration** (`scripts/analysis/run_daily_refresh.py`):
  - New `stage1_ou_cache_alt_a` step runs the builder with
    `--smoothing-mode empirical_when_available --out
    cache/mlb_ou_cache_alt_a.staging.json`. Same history window
    (`--min-season` / `--max-season`) as the production Stage-1
    step.
  - **NEVER auto-promoted**: no companion inline promote step.
    Operator runs `promote.py stage1 --source
    cache/mlb_ou_cache_alt_a.staging.json` manually after paper-
    mode validation clears its bar.
  - Same `StalenessCheck` on `data/games/regular/` as the production
    step, so the Alt-A rebuild only fires when game data changes.

  **Daily-review block**
  (`scripts/analysis/build_daily_human_review_report.py`):
  - New `_stage1_alt_a_staging_health` reads the staging cache +
    surfaces: existence, history window, total_games, valid_cells,
    `alt_a_smoothing` summary (cells_overridden + per-line counts +
    mean_signed_delta), lineage (built_at_utc + git_sha +
    builder_path), build_age_days, and **cross-cache input-hash
    divergence vs the production cache**.
  - Alerts on: (1) missing staging cache, (2) corrupt JSON, (3)
    `built_at_utc` > 14d ago, (4) staging built in `poisson` mode
    instead of Alt-A (operator typo), (5) staging + production
    disagree on `data/games/regular/` input hash (one of them is
    built on a stale corpus).
  - Mirrors alerts via top-level Notes with prefix
    `Stage1-alt-a-staging:`.

  **First live production build** (2026-05-18, full 5-season window):
  - **4,200 of 4,298 cells (97.7%) had `poXX` overwritten with
    `oXX`**.
  - **mean_signed_delta = -0.0274 (2.74pp lower than production)** --
    Alt-A is systematically more conservative, exactly the direction
    needed to reduce the +27pp over-prediction bias.
  - mean_abs_delta_logit = 0.84 -- the Poisson smoothing was
    pulling probabilities meaningfully away from empirical in
    logit space.
  - 21,240 individual (cell, line) overrides recorded across 6
    lines (po65 / po75 / po85 / po95 / po105 / po115).
  - 3,568 (cell, line) pairs kept Poisson because empirical was at
    the 0/1 boundary; the Poisson smoothing's value here.
  - 0 alerts fired in the daily-review block; 0 input divergences
    vs the current production cache (both share the same
    `data/games/regular/` hash).

  **What this unlocks**: the 7-day paper-mode validation runway
  starting today (per yesterday's CLI command shipment) accumulates
  shadow Alt-A diagnostics on every candidate. When the operator is
  ready to flip Active #8 to ENFORCE, the workflow becomes one
  command: `python scripts/analysis/promote.py stage1 --source
  cache/mlb_ou_cache_alt_a.staging.json`. The promote.py stage1
  subcommand (shipped yesterday) handles the atomic swap + backup
  + audit row + post-flip ops checklist; the daily refresh keeps
  the staging artifact current; the daily review surfaces its
  freshness and override stats.

  **What v2 should add** (deferred; not blocking):
  - **Cell-level diff report** in `data/analysis_output/` that
    surfaces the top-N cells with the largest poisson->empirical
    deltas so the operator can spot-check the override before
    promoting (the cell-conditional drill from yesterday partially
    answers this on the bet-side; this would do it on the
    cell-side).
  - **Auto-recommend** in the daily review: after N consecutive
    days of stable Alt-A staging + paper-mode bias-reduction
    confirmed, surface a "you may now promote" notes line.

  **Files**: `cache/build_mlb_ou_cache.py` (+~120 LOC: new flags,
  `_apply_alt_a_smoothing` helper, meta block wiring, lineage
  cli_args_summary extension, end-of-run print summary),
  `scripts/analysis/run_daily_refresh.py` (new
  `stage1_ou_cache_alt_a` RefreshStep + StalenessCheck),
  `scripts/analysis/build_daily_human_review_report.py`
  (`DEFAULT_STAGE1_ALT_A_STAGING_PATH` constant, new
  `_stage1_alt_a_staging_health` block + `build_report` +
  `_build_notes` + return-dict wiring),
  `tests/test_build_mlb_ou_cache.py` (+5 builder tests),
  `tests/test_run_daily_refresh.py` (+2 refresh-step tests),
  `tests/test_build_daily_human_review_report.py` (+8 health-block
  tests). **1295 tests + 41 subtests pass.**

- **Active #16 v4: cross-artifact consistency check** *(2026-05-17)* --
  the natural next consumer of today's v2 + v3 lineage work. V2
  stamped `input_hashes` on 6 builders. V3 surfaced per-artifact
  build-freshness in the daily review. V4 surfaces the
  DEPENDENCY GRAPH: when a downstream artifact was built against a
  version of an upstream input that has since been updated by a
  refresh, the operator sees the stale relationship before it
  poisons evidence.

  **Two alert classes**:
  - **Per-artifact stale**: a single artifact's
    `input_hashes[X]` differs from the current hash of file X.
    The artifact's evidence is built on a version of the input
    that no longer exists.
  - **Cross-artifact divergence**: two artifacts share an input
    path but recorded different hashes for it. One was built
    before a refresh updated the input; the other after.
    Surfaces which artifact carries the stale evidence.

  **Helper** (`scripts/analysis/artifact_lineage.py`):
  - `compare_input_hash(lineage, input_path, project_root)` reads
    the recorded hash + computes current via the existing
    `hash_file` + classifies (match / stale / not_tracked /
    current_missing). Path resolution: tries repo-relative key
    first (the canonical storage form) then falls back to
    as-passed string.
  - New constants: `CONSISTENCY_MATCH`, `CONSISTENCY_STALE`,
    `CONSISTENCY_NOT_TRACKED`, `CONSISTENCY_CURRENT_MISSING`.
  - Fail-open: malformed lineage or non-dict input returns
    `not_tracked` rather than raising.

  **Daily-review block**
  (`scripts/analysis/build_daily_human_review_report.py`):
  - New `_cross_artifact_consistency_health` runs over a
    configured `CROSS_ARTIFACT_CONSISTENCY_PATHS` tuple covering
    10 artifacts: Stage-1 cache, Stage-2 cache, Stage-3 v2
    weights, calibrator OVER + UNDER, walk-forward cert,
    EV-policy report, loss-attribution, stage1_shadow_override,
    stage1_cell_loss_attribution.
  - Two-pass algorithm: pass 1 builds per-(artifact, input)
    verdicts and indexes `inputs_seen[ip] -> list of
    (label, recorded_hash, current_hash)`. Pass 2 walks
    inputs_seen, groups by recorded_hash, flags any input with
    >= 2 distinct recorded hashes as a divergence.
  - Artifacts missing / pre-V2 / read errors are tagged but
    don't alert (already surfaced by
    `cache_lineage_freshness_health`).
  - Alerts mirror to top-level Notes with prefix
    `Cross-artifact:`.

  **First production run on 2026-05-17** (10 artifacts checked):
  - 5 artifacts have full lineage with all inputs MATCH:
    `calibrator_over`, `calibrator_under`, `walk_forward_cert`,
    `stage1_shadow_override`, `stage1_cell_loss_attribution`.
  - 4 artifacts are `no_lineage_pre_v2` (Stage-1 cache, Stage-2
    cache, EV-policy report, loss-attribution): built before
    today's v2 ship; will get lineage on next refresh.
  - 1 artifact `missing` (Stage-3 v2 weights -- never promoted).
  - **0 alerts** -- consistent state.

  **Why this is the right next ship after today's paper-mode
  switch**: a full week of paper-mode data refreshes will
  exercise the dependency graph multiple times. Any inconsistency
  introduced by a partial refresh, a flaky daemon promotion, or
  a hand-edit will show up as a stale alert within 24h. Without
  this check, the operator could be staring at calibrator
  recommendations built against a Stage-1 cache that the daily
  refresh has since updated.

  **What v5 should add** (deferred; not blocking):
  - **Transitive consistency**: if Stage-2 was built against
    Stage-1 sha X but Stage-1 is now sha Y, the calibrator
    (which depends on Stage-2 outputs through the training
    table) is transitively stale. v4 only checks DIRECT input
    matches; v5 could walk the dependency graph.
  - **Auto-rebuild trigger**: when N consecutive refreshes show
    the same stale relationship, surface a "this artifact's
    rebuild step has been silently failing" alert.

  **Files**: `scripts/analysis/artifact_lineage.py` (+115 LOC:
  new constants + `compare_input_hash` helper),
  `scripts/analysis/build_daily_human_review_report.py` (new
  `CROSS_ARTIFACT_CONSISTENCY_PATHS` constant + new
  `_cross_artifact_consistency_health` block + `build_report`
  wiring + `_build_notes` `Cross-artifact:` prefix + return
  dict key), new
  `tests/test_cross_artifact_consistency.py` (8 helper tests
  covering all 4 status outcomes + repo-relative key resolution
  + malformed lineage + 8 daily-review block tests covering
  all-missing / pre-V2 / all-match / stale-fires-alert /
  cross-divergence / two-agreeing / disjoint-inputs / corrupted
  artifact JSON / Notes-mirror prefix). **1280 tests + 41
  subtests pass.**

- **Active #14: backup retention + PSI-history GC** *(2026-05-17)* --
  closes a documented hygiene gap that became more pressing today
  with the 5th promotion lever (Stage-1) shipping. Two pieces:

  **Backup archive + GC (`promote.py`)**:
  - Each promotion calls `_backup_prior_production` to copy current
    production -> `<file>.prior_promote.json` (the "latest backup"
    that `demote` reads). Before today, each new promotion silently
    OVERWROTE the prior backup -- so the operator could only roll
    back ONE step.
  - New behavior: BEFORE writing the new backup, the existing
    `.prior_promote.json` is rotated into a sibling
    `<file>.prior_promote_archive/<YYYYMMDDTHHMMSSZ>.json` directory
    (timestamp = the prior backup's mtime, preserving "when was this
    captured" semantics). After the new backup is safely on disk,
    the archive is GC'd to `BACKUP_ARCHIVE_KEEP=5` most-recent
    entries.
  - **Preserves backward compatibility**: the `.prior_promote.json`
    single-file contract that `demote` reads is unchanged; the
    archive is purely additive. Existing tests + the audit-row
    `backup_path` contract still work.
  - **Disambiguation**: if two rotations land in the same UTC
    second (fast-rerun tests), the archive filename gets a `_N`
    suffix so no entry is lost.
  - **Fail-open**: rotation + GC both wrapped in try/except so a
    permissions error / filesystem issue can never block the
    promotion path. The new backup write still succeeds.

  **PSI-history trim (`build_concept_drift_report.py`)**:
  - `psi_history.jsonl` grows at ~7 features/day = ~2.5k rows/year.
    Drift-in-drift only consumes the trailing 30d; older rows are
    pure storage cost. Trim is overdue.
  - New behavior: after every append, `_trim_psi_history` rewrites
    the file keeping only rows whose `active_date` falls within
    `PSI_HISTORY_RETENTION_DAYS=365` of the LATEST date in the
    file. Anchoring on latest (not today) keeps the trim stable
    when the refresh runs against a stale corpus.
  - **Atomic rewrite via tmp file** so a crash mid-trim can't
    corrupt the history.
  - **Cleans corrupted lines** opportunistically: when the parse
    encounters non-JSON lines, the rewrite drops them (counter +
    rewrite-anyway logic, not silent-noop). Prevents accumulation
    of garbage from interrupted writes.
  - **Unparseable `active_date` rows kept**: we don't drop data
    just because we can't classify it. The trim is data-loss-averse.
  - **Fail-open** throughout: any OSError silently swallowed; the
    next append-and-trim cycle gets a fresh chance.

  **What today's 10-shipment count means for backup volume**:
  - 5 levers x 1 promotion cycle = 5 backup files in the cache
    folder. Below today, that grew by 1 per promotion (overwriting
    the prior). Now each lever can accumulate up to 6 files
    (1 current + 5 archive), so total cap = 30 backup files across
    all 5 levers.
  - For PSI history: today's file is ~7 rows/day x ~10 days
    elapsed = ~70 rows. The trim is a no-op until the file
    accumulates 365+ days of data; testing on the live file would
    leave it untouched.

  **Files**: `scripts/analysis/promote.py` (+155 LOC: new
  `BACKUP_ARCHIVE_KEEP` constant, `_archive_dir_for`,
  `_list_archive_backups`, `_gc_archive_backups`,
  `_rotate_existing_backup_to_archive` helpers; modified
  `_backup_prior_production` to rotate-then-write-then-gc with
  fail-open wrappers),
  `scripts/analysis/build_concept_drift_report.py` (new
  `PSI_HISTORY_RETENTION_DAYS` constant, `_trim_psi_history`
  helper, `_write_history_rows` call site). 24 new tests in
  `test_backup_retention_and_psi_history_gc.py` covering 4 helper
  classes + 7 trim-history cases including corruption + stale-date
  anchoring + atomic rewrite verification.
  **1262 tests + 41 subtests pass.**

- **`promote.py stage1` + `demote stage1` subcommands** *(2026-05-17)* --
  closes the last gap in the promote.py coverage matrix. Before this
  ship, 4 of the 5 major levers (stage2, stage3-v2, stake-scaling,
  gate-threshold) flowed through the auditable promote.py path while
  Stage-1 was the only one without promote/demote tooling. Today's
  Stage-1 Alt A runtime shadow logging (shipped earlier) is the
  active workstream toward an eventual ENFORCE flip; that promotion
  now has a standard auditable path to flow through.

  **Verdict model** (different from Stage-2's Brier-history-driven
  verdict because the Stage-1 cache has no validation Brier):
  - `promote` -- source file exists AND its `lineage.built_at_utc`
    is at or after production's
  - `staging_missing` -- source file absent (the operator hasn't
    written a candidate cache to the staging path)
  - `production_missing` -- first-time promotion; allowed (no
    backup to take, but no downgrade risk either)
  - `source_older_than_production` -- production was built later;
    refused without `--force` to prevent silent downgrade
  - `no_lineage_comparison` -- one or both files lack lineage; allow
    with `--force` after operator inspection

  **`cmd_stage1` semantics** (mirrors `cmd_stage2`):
  - Reads verdict, prints it with source/production built_at_utc
    timestamps + reason text
  - `--dry-run`: writes a dry_run audit row, no file action
  - Blocked without `--force` when verdict isn't `promote`
  - On promote: backs up current production atomically to
    `<file>.prior_promote.json` (via the existing
    `_backup_prior_production` helper), then atomic-copies source
    to production, writes a `promoted` (or `forced`) audit row
    stamped with source-artifact lineage + promotion-time
    lineage.
  - Operator checklist prints next-step actions: restart engine,
    watch cohort_calibration_health + loss_attribution_health for
    bias reduction, fast Wilson-UB demote is now armed.

  **`cmd_demote_stage1` semantics** (mirrors `cmd_demote_stage2`):
  - Computes outcome-regression verdict via the standard
    pre/post 14d ROI window (`stage1_demotion_verdict`)
  - Restores from `backup_path` recorded on the original
    promotion event when present
  - Falls back to deleting current production when no backup
    exists (next refresh's `stage1_ou_cache` step rebuilds from
    `data/games/`)

  **Verdict helpers added**:
  - `_stage1_promotion_verdict(source_path, production_path)` --
    the 5-outcome decision tree above
  - `stage1_demotion_verdict(events, sessions_dir)` -- windowed
    Wilson-UB-style outcome regression, same shape as the other
    4 levers
  - `stage1_fast_demote_verdict(events, sessions_dir)` -- fast
    Wilson UB parallel check (Active #13 pattern); fires within
    5-6 days of a bad promotion

  **CLI registration**:
  - New `stage1` subparser under top-level `sub` (default source
    path: `cache/mlb_ou_cache.staging.json`)
  - New `demote stage1` subparser under the existing `demote`
    subparser
  - `cmd_status` extended to print stage1 verdict alongside the
    other 4 + include stage1 in the demote + fast-demote verdict
    dictionaries

  **CLI smoke test** on production data:
  ```
  $ python scripts/analysis/promote.py status
  [stage1] verdict: no_lineage_comparison
    source path: mlb_ou_cache.staging.json
    source built_at_utc: <not present>
    production built_at_utc: <not present>
    reason: One or both files lack a `lineage.built_at_utc` field. ...
  ```
  Today's production caches predate the v2 lineage stamping; after
  the next refresh rebuilds Stage-1, both files will carry lineage
  and the verdict will be actionable (`promote` when the operator
  ships a fresh staging cache).

  **Direct prereq for Active #8**: when Stage-1 Alt A's 30-day
  shadow window clears and the operator chooses to swap the
  Stage-1 cache (or after a deeper future cache rebuild), the
  promotion now flows through the same atomic-swap + backup +
  lineage-stamp + audit-row pipeline as Stage-2/Stage-3. The
  ENFORCE flip is a single `promote.py stage1` invocation.

  **Files**: `scripts/analysis/promote.py` (+450 LOC: 2 new
  default-path constants, `_read_lineage_built_at` + 3 verdict
  helpers, `cmd_stage1` + `cmd_demote_stage1`, defensive getattr
  in `cmd_status` for backward-compat with existing
  SimpleNamespace-based test fixtures, 3 subparser registrations
  including the stage1 args on the status subparser),
  `tests/test_promote_stage1_subcommand.py` (20 tests covering all
  5 verdict outcomes, dry-run / blocked / forced / successful
  promotion flow with backup + audit row stamping, first-promotion
  no-backup path, demote restore-from-backup + fallback-delete
  paths, demotion verdict smoke, subcommand registration).
  **1238 tests + 41 subtests pass.**

- **Stage-1 Alt A runtime shadow logging (Active #8 prep)** *(2026-05-17)* --
  the production-runtime layer that completes today's
  observability → narrowing → shadow-evidence arc. This morning's
  shadow-override report computes Alt A counterfactuals from settled
  bets only (~24/week); this runtime hook captures alt FVs on EVERY
  candidate evaluation (~5000/day) so the eventual Active #8 ENFORCE
  flip has 200x+ more evidence to weigh.

  **Code path** (touches 4 modules):
  - **CLI flag**: `--stage1-shadow-empirical-override {off, shadow}`
    on `scripts/trading/live_engine_cli.py` (default `off`; operator
    opts in explicitly).
  - **Bridge**: `LiveTradingEngine.__init__` in
    `scripts/trading/live_engine.py` copies
    `live_args.stage1_shadow_empirical_override` ->
    `trade_args.stage1_shadow_empirical_mode` before delegating to
    SignalEngine. The explicit bridge keeps the SignalEngine
    contract clean (engine reads only from trade_args).
  - **Engine wiring**: `SignalEngine.__init__` parses
    `trade_args.stage1_shadow_empirical_mode`, validates against
    {off, shadow}, logs the mode at INFO if shadow is active.
  - **Runtime computation**: new
    `_attach_stage1_shadow_empirical_fields` helper in
    `scripts/trading/signal_pipeline_gates_post_fv.py` runs right
    after `fair_value` is computed but before post-FV gates. When
    mode=shadow AND cell empirical is present (0 < empirical < 1):
    `p2_alt = sigmoid(logit(empirical) + s2_delta + s3_delta)`,
    then run the SAME production calibrator on p2_alt to get
    p3_alt. Logs both prod + alt FVs on the candidate row.
  - **Fail-open contract**: ANY exception in the alt path is logged
    at DEBUG and silently swallowed; production fair_value is the
    sole source of truth for decisions. Mode `off` is a fast path
    that attaches only the mode tag (no math, no calibrator call).

  **Schema additions to candidate row** (and pulled through into
  `signal_training_table.jsonl` via
  `scripts/analysis/build_signal_training_table.py`):
  - `stage1_shadow_empirical_mode` -- `'off'` / `'shadow'`
  - `fair_value_alt_empirical` -- alt p3 (post calibration); None
    when mode=off or no empirical
  - `fair_value_alt_empirical_raw` -- alt p2 (pre calibration)
  - `fair_value_alt_empirical_delta_vs_prod` -- alt_p3 - prod_p3
    (signed; negative = alt is more conservative)
  - `fair_value_alt_empirical_used_empirical` -- True/False; the
    flag that distinguishes "shadow ran AND used empirical" from
    "shadow ran but empirical unavailable / unusable"
  - `fair_value_alt_empirical_p0` -- the empirical input that was
    substituted for Stage-1's Poisson estimate

  **Offline report integration**:
  `build_stage1_shadow_override_report.py` now PREFERS the runtime-
  logged `fair_value_alt_empirical` over its own offline logit-
  additive fallback. The `alt_a_source` field on each ShadowBet
  distinguishes `runtime` / `offline` / `no_change`; aggregate
  payload surfaces an `alt_source_breakdown` so the operator can see
  the runtime-coverage rollover as the flag accumulates production
  data. Until the operator enables the flag, the report falls back
  to offline computation (same behavior as before).

  **Operator path forward**: enable the flag tomorrow with
  `--stage1-shadow-empirical-override shadow`. The next 30 days of
  candidate logs accumulate per-tick alt evidence. The daily
  shadow-override report's `Stage1-shadow:` Notes alert
  automatically picks up the runtime data and (if the trend holds)
  continues to recommend `promote_to_runtime_shadow`. After 30
  clean days of evidence, Active #8's ENFORCE flip is a **single
  config-file edit** (flag from `shadow` to `enforce` plus a
  one-line change in the runtime to USE the alt FV for the trade
  decision instead of just logging it).

  **No production behavior change today**. With the flag at its
  default `off`, the engine runs identically to yesterday. The
  only operator-visible difference is `stage1_shadow_empirical_mode:
  off` showing on each candidate row.

  **Files**: `scripts/trading/live_engine_cli.py` (new CLI flag),
  `scripts/trading/live_engine.py` (explicit live_args -> trade_args
  bridge in `__init__`),
  `scripts/trading/signal_engine.py` (parse + validate + log mode),
  `scripts/trading/signal_pipeline_gates_post_fv.py` (new
  `_attach_stage1_shadow_empirical_fields` helper +
  `_stage1_shadow_logit/_stage1_shadow_sigmoid` math primitives +
  call site after fair_value computation),
  `scripts/analysis/build_signal_training_table.py` (5 new
  PRE_SIGNAL_COLUMNS),
  `scripts/analysis/build_stage1_shadow_override_report.py`
  (`alt_a_source` field on ShadowBet + runtime preference logic in
  `project_bet` + `alt_source_breakdown` in aggregate),
  `tests/test_signal_engine_phase1_characterization.py` (golden
  fixture extended with the new fields when mode=off). 20 new tests
  in `test_stage1_shadow_empirical_runtime.py` (mode=off attaches
  only tag + skips calibrator, mode=shadow without empirical sets
  only tag, mode=shadow with empirical computes correct logit-
  additive math + delta vs prod, calibrator called on alt raw,
  calibrator shift propagates, fail-open on calibrator raises, mode
  attr missing treated as off, logit/sigmoid roundtrip, live_args
  bridge contract, CLI registration end-to-end). 5 new tests in
  `RuntimeAltSourcePreferenceTests` (runtime preferred over offline,
  offline fallback when runtime not logged, no_change when neither
  available, runtime-with-used_empirical=False falls back to
  offline, source breakdown in aggregate). **1218 tests + 41
  subtests pass.**

- **Stage-1 shadow-override report (Active #8 prep)** *(2026-05-17)* --
  the shadow-first evidence layer that precedes the Active #8 runtime
  change. Today's cell-conditional drill identified two specific
  candidate fixes (Alt A: prefer empirical-when-available, Alt B:
  fail-closed on `fallback_level >= 2`). This shipment replays both
  alts against actual training-table outcomes so the operator sees
  the counterfactual impact BEFORE changing live FV math.

  **Pattern**: this is the same "shadow-first then promote" pattern
  the rest of the codebase uses for every risky live-runtime change
  (no-score drift, EV policy, stake scaling, two-sided quote engine).
  After this report shows durable improvement over a clean 30d
  window, Active #8 promotes the alt to live behind a runtime flag.

  **Math** (in `scripts/analysis/build_stage1_shadow_override_report.py`):
  For each filled+settled bet, compute production p3 + both alt
  counterfactuals via the logit-additive chain (verified to 0.001
  on all 87 production bets earlier today):
  - p0_poisson = `base_fair_value` (production Stage-1)
  - p0_empirical = `inferred_state_base_empirical` (when present)
  - p3_alt_A = `sigmoid(logit(p0_empirical) + s2 + s3)` when
    empirical is in (0, 1); else p3_alt_A = p3_prod (no change)
  - alt_B_kept = NOT (`fallback_level >= 2`); when False, the bet
    would not have been placed at all
  - bias_X = p3_X - won (signed; positive = over-prediction)

  **Aggregation** per window (all / trailing_30d / trailing_7d):
  - Production aggregate: mean_p3, mean_won, bias, total_profit
  - Alt A: mean_p3, bias, n_changed, coverage_rate, **bias_delta_vs_prod_pp**
    (the load-bearing improvement metric)
  - Alt B: n_blocked, n_kept, kept_bias, blocked W/L split,
    **counterfactual_profit_delta_usd** (the $-impact of blocking)

  **Recommendations** auto-fire when alt evidence clears the floor:
  - Alt A: bias improvement >= 1pp AND coverage_rate >= 25% AND
    n_total >= 30
  - Alt B: counterfactual_profit_delta >= $20 AND n_blocked >= 3 AND
    n_total >= 30

  **First production run on 2026-05-17** across 87 settled bets
  (trailing-30d window):
  - Production aggregate bias: **+27.1pp** (the value Active #10
    surfaced this morning).
  - **Alt A: bias drops to +21.2pp** (6.0pp improvement, applied
    to 32 of 87 bets = 37% coverage). The reduction is concentrated
    where empirical data exists; if empirical coverage were 100%,
    the bias reduction would extrapolate to ~16pp (the per-cell
    poisson-empirical gap the drill measured earlier).
  - **Recommendation fires for Alt A**: `promote_to_runtime_shadow`.
    The operator's next-session action is now well-evidenced.
  - **Alt B: blocks 6 bets (3W/3L)**, counterfactual delta = +$15.
    Just below the $20 recommendation threshold -- evidence is real
    but thin; let it accumulate for another week before acting.

  **Daily-review block** `_stage1_shadow_override_health`
  reads the artifact and mirrors any firing recommendation to
  top-level Notes with prefix `Stage1-shadow:`. Surfaces the
  recommendation's full rationale (not just the verdict tag) so
  the operator can read the evidence in one line.

  **What v2 should add**:
  - **Runtime shadow logging**: hook into
    `signal_pipeline_gates_post_fv.py` to log p3_alt_A alongside
    p3_prod on every candidate decision (not just settled bets).
    Lets the operator A/B audit decisions BEFORE outcomes settle.
  - **Per-cohort alt impact**: extend the report with the standard
    cohort cuts (edge / ask / inning / line / cse) so the operator
    can see where Alt A's improvement is concentrated.
  - **Alt C (Poisson with weight cap on deep fallback)**: shrink
    the Poisson estimate toward 0.5 at high fallback levels rather
    than blocking outright. May produce a smoother fix than Alt B.

  **Files**: new `scripts/analysis/build_stage1_shadow_override_report.py`
  (~545 LOC: ShadowBet projection with pre-computed alt
  counterfactuals, window slicing, aggregation, recommendation
  thresholds, markdown render, lineage stamping),
  `scripts/analysis/run_daily_refresh.py` (new
  `stage1_shadow_override_report` refresh step right before the
  cell-conditional drill so it consumes the same training table
  freshly),
  `scripts/analysis/build_daily_human_review_report.py` (new
  `_stage1_shadow_override_health` block + `Stage1-shadow:` Notes
  mirror + build_report wiring), 25 new tests in
  `test_build_stage1_shadow_override_report.py` (projection
  filter, alt-A math identity verification, alt-B threshold
  semantics, aggregate math including counterfactual P&L direction,
  recommendation threshold fires/suppresses, window slicing, schema
  + markdown + empty input), 7 new tests in
  `Stage1ShadowOverrideHealthTests` (artifact loading, no-rec /
  rec-mirror semantics, schema completeness, stale artifact,
  Notes mirror prefix). **1193 tests + 41 subtests pass.**

  **Strategic implication for Active #8**: the rebuild surface has
  now narrowed three times today:
  - Original spec: "rebuild Stage-2 + Stage-3 v2"
  - After Active #10: "rebuild Stage-1 (not Stage-2/3)"
  - After cell-conditional drill: "fix Stage-1 smoothing toward
    empirical AND tighten fallback gating"
  - After this shadow-override report: **"ship Alt A
    (empirical-when-available) to runtime shadow first; Alt B
    needs another week of evidence before action"**

  Active #8 next session is now a **targeted runtime change**:
  add a feature flag to the Stage-1 lookup that prefers empirical
  when present, log both production and alt FVs in shadow, and
  let this report's daily output show the cumulative shadow
  improvement. No full cache rebuild needed.

- **Stage-1 cell-conditional loss attribution** *(2026-05-17)* --
  the natural drill-down to today's Active #10 shipment. Active
  #10 told us "Stage-1 owns ~100% of the 27pp bias" at the
  stage level; this report drills the SAME bets across Stage-1's
  INTERNAL cohort dimensions so the operator can see WHICH KIND
  of Stage-1 cells are responsible BEFORE Active #8 fires the
  rebuild.

  **Cohort dimensions (5 Stage-1-internal cuts)**:
  - `stage1_fallback_level_bucket` -- 0 (exact cell) /
    1 (one-level fallback) / 2+ (deeper fallback) / missing
  - `stage1_line_fallback_mode_bucket` -- exact / extrapolate_*
    / interpolate / missing
  - `stage1_used_fallback_bucket` -- True / False (did the runtime
    lookup land on a fallback at all?)
  - `stage1_n_bucket` -- <50 / 50-200 / 200-1000 / >=1000
    (cell sample-size support)
  - `stage1_poisson_empirical_gap_bucket` -- abs(poisson -
    empirical) bucketed at <0.05 / 0.05-0.10 / 0.10-0.20 / >=0.20

  **Per-cohort metrics**: n, mean_p0, mean_won, stage1_bias
  (signed), mean_poisson_minus_empirical (the smoking gun
  metric -- when present, shows how much the Poisson smoothing
  inflates above the cell's own historical empirical rate),
  fallback_rate, mean cell sample size.

  **Top-culprits ranking** flags cohorts where:
  - n >= 5 (cohort floor)
  - |stage1_bias| >= 5pp (material)
  - share of aggregate bias (= cohort_bias / aggregate_bias) >= 25%
  Helpful cohorts (negative shift in the bias direction) get
  excluded so the operator only sees cohorts that HURT the model.
  Ratio can exceed 1.0 (cohort amplifies aggregate); the rationale
  text explains the amplification.

  **Three windows** (all / trailing_30d / trailing_7d) match the
  rest of the drift family.

  **First production run on 2026-05-17** across 88 settled bets:
  - **Trailing-30d Stage-1 bias: +28pp** (mean_p0=92.7%,
    mean_won=64.8%) -- consistent with Active #10's stage-level
    finding.
  - **fallback_rate=69%** -- 60+ of 88 bets landed on fallback
    cells. The headline driver: the bot is mostly betting in
    cells where the runtime knew it was on shaky ground.
  - **mean(poisson - empirical)=+16pp** across 32 cells where
    both estimates are available. The Stage-1 Poisson smoothing
    systematically inflates the probability above the cell's
    OWN historical empirical rate by 16pp. **This is the
    smoking-gun fix target**: revising the smoothing toward
    empirical-when-available would close ~16pp of the 28pp gap.
  - **Top culprit cohort**: `stage1_fallback_level_bucket=level_2plus_fallback`
    with stage1_bias=+40pp, n=6, ratio_vs_aggregate=1.44x. When
    the runtime falls back TWO or more levels, the over-prediction
    is even worse.
  - 10 cohort culprits cleared the thresholds; most cluster
    around the same theme (fallback cells + non-trivial Poisson-
    empirical gaps).

  **Daily-review block** `_stage1_cell_loss_health` reads the
  artifact and mirrors a one-line Notes alert with prefix
  `Stage1-cell-loss:` when |aggregate bias| >= 5pp AND
  fallback_rate >= 50%. Surfaces the top culprit cohort + the
  Poisson-empirical gap signal. Sample alert:
  ```
  Stage1-cell-loss: trailing-30d Stage-1 bias +28.0pp on n=88
  bets with fallback_rate=69% -- Active #8 retrain surface
  narrows to the Stage-1 fallback path. Top culprit:
  `stage1_fallback_level_bucket=level_2plus_fallback`
  (bias +40.2pp, n=6, ratio_vs_agg=1.44x). Poisson smoothing
  diverges from empirical by +16.1pp on average -- candidate
  fix is the Stage-1 smoothing, not the fallback path.
  ```

  **Strategic implication**: Active #8 originally specced a
  full Stage-2 + Stage-3 rebuild; Active #10 narrowed that to
  Stage-1; THIS shipment narrows further to two specific
  candidate fixes:
  1. **Tighten the fallback path** (fallback_rate is 69% on our
     bet selection -- either require deeper fallback to fail
     closed, or weight the Poisson estimate down at higher
     fallback levels).
  2. **Revise the Poisson smoothing** (smoothing inflates by
     +16pp vs empirical -- shift the runtime lookup toward
     empirical-when-available, or reduce the Poisson prior
     weight on low-n cells).
  The 27pp aggregate bias is approximately decomposable as
  (16pp smoothing + ~12pp fallback-path amplification), so
  both fixes would compound to close most of it.

  **Files**: new `scripts/analysis/build_stage1_cell_loss_attribution.py`
  (~520 LOC: `Stage1Bet` dataclass + projection from training
  table, 5 cohort bucketers, aggregation math, top-culprits
  ranking with helpful-cohort exclusion + amplification ratio,
  3-window slicing, markdown render, end-to-end main with
  lineage stamping),
  `scripts/analysis/run_daily_refresh.py` (new
  `stage1_cell_loss_attribution` refresh step right before the
  bet-level loss attribution step so the cohort drill is fresh
  when the operator reads the daily review),
  `scripts/analysis/build_daily_human_review_report.py` (new
  `_stage1_cell_loss_health` block + `Stage1-cell-loss:` Notes
  mirror + build_report wiring), 32 new tests in
  `test_build_stage1_cell_loss_attribution.py` (projection
  filter, bucketing semantics, aggregate math, top-culprits
  ranking + ratio_can_exceed_one + helpful-cohort exclusion +
  sort order + rationale, window slicing, schema + markdown +
  empty input), 8 new tests in `Stage1CellLossHealthTests`
  (artifact loading, alert firing on high bias + high fallback
  rate, suppression on low bias / low fallback rate / empty
  window, stale artifact, schema, Notes mirror).
  **1161 tests + 41 subtests pass.**

- **Active #16 v3: lineage visibility (startup log + daily review)** *(2026-05-17)* --
  closes the v3 follow-ups documented this morning. V1 stamped
  lineage on calibration artifacts + 4 promote.py audit rows; V2
  extended that to the Stage-1/Stage-2/Stage-3-v2/EV-policy
  builders. V3 makes that lineage **operationally visible**:
  - **At engine boot**: every cache load (Stage-1, Stage-2,
    Stage-3 v2 weights, calibrator) now writes a one-line INFO
    log entry summarising the artifact's lineage. The operator
    can grep `Artifact lineage:` in the runtime log to answer
    "which version was live during this session" in seconds.
  - **In the daily review**: a new `cache_lineage_freshness_health`
    block reads each cache's embedded lineage and surfaces a
    per-artifact panel (built_at_utc, build_age_days, git_sha,
    git_dirty, builder_path, input counts). Fires a stale-cache
    alert when build_age > 14d AND the lineage is present;
    pre-V2 artifacts (no lineage block yet) surface as a status,
    not an alert (they'll get lineage on next refresh).

  **Reusable helpers in `artifact_lineage.py`**:
  - `_read_lineage_from_path(path)` -- best-effort artifact
    JSON reader returning the `lineage` dict or None.
  - `_age_days(iso_ts)` -- timezone-aware ISO timestamp to days
    elapsed; tolerant of missing/bad input.
  - `format_lineage_summary_line(label, lineage)` -- canonical
    one-line summary used by both the startup logger and the
    daily-review panel so the operator's eye learns one shape.

  **Engine integration** (`scripts/trading/signal_engine.py`):
  new top-level `_log_artifact_lineage_summary(label, path,
  expected=True)` helper called once per cache load. Fail-open
  contract: ANY error reading lineage is logged at DEBUG and
  silently swallowed so startup never blocks on a lineage read.
  Wired into the four SignalEngine.__init__ load sites
  (stage1_cache, stage2_cache, stage3_v2_weights, calibrator).
  The Stage-3 v2 weights call passes `expected=False` because
  the v2 weights JSON is an optional override; absence is not
  an error.

  **Daily-review block** (`build_daily_human_review_report.py`):
  new `_cache_lineage_freshness_health` reads 5 artifacts
  (stage1, stage2, stage3-v2, calibrator OVER + UNDER) and
  produces per-artifact info (status / built_at_utc /
  build_age_days / git_sha / summary line). Alerts fire when
  required artifacts are missing or when their build_age
  exceeds the configurable warn threshold (default 14d).
  Pre-V2 artifacts are tagged `no_lineage_pre_v2` with no
  alert; they'll auto-resolve on next refresh. Mirrored to
  Notes with prefix `Cache-lineage:`.

  **First production smoke test on 2026-05-17**:
  - `calibrator_over`: `built=2026-05-17T19:31:55Z(0.8d ago)
    git=0840bb7c1cac(dirty)` -- proper lineage (v1 stamped
    earlier today).
  - `calibrator_under`: same shape, `built=2026-05-17T19:37:37Z`.
  - `stage1_cache`: `no_lineage_pre_v2` (built before v2 wiring;
    will get lineage on next refresh).
  - `stage2_cache`: `no_lineage_pre_v2`.
  - `stage3_v2_weights`: `missing_optional` (artifact not
    promoted to production yet).
  - 0 alerts fired (no stale lineage; pre-V2 status is informational).

  **Operational implication**: once today's pre-V2 caches
  rebuild (next refresh), the operator will be able to see in
  ONE GLANCE in the daily-review markdown whether each cache is
  fresh, on what data, and with what git_sha. The startup log
  line provides the same info during the live session. Combined
  with the existing `promote.py status` (which already shows
  lineage from v1), the operator has full audit trail coverage
  for "which artifact, when built, by what code, when promoted."

  **What's still deferred to v4** (smaller follow-ups; no
  binding need today):
  - Lineage-aware `promote.py demote` UX: surface "demoting
    from sha X built N days ago back to sha Y from N+M days
    ago." Already partially shipped (`promote.py status`
    surfaces this) but the demote command itself doesn't echo
    it in the confirmation flow.
  - Cross-artifact consistency check: when calibrator was
    built against an older Stage-1 cache than the one in
    production, flag the mismatch. Becomes meaningful once a
    few rebuilds have shipped.

  **Files**: `scripts/analysis/artifact_lineage.py` (~85 new
  LOC: `_read_lineage_from_path`, `_age_days`,
  `format_lineage_summary_line`),
  `scripts/trading/signal_engine.py` (new
  `_log_artifact_lineage_summary` helper + 4 call sites in
  `__init__`),
  `scripts/analysis/build_daily_human_review_report.py` (new
  `CACHE_LINEAGE_*` constants, new `_cache_lineage_freshness_health`
  block, `_build_notes` mirror with `Cache-lineage:` prefix,
  `build_report` wiring + return dict key). 15 new tests in
  `test_lineage_v3_startup_logging.py` (lineage summary
  formatting, path reading, age calculation, startup helper
  fail-open contract end-to-end including log capture). 7 new
  tests in `CacheLineageFreshnessHealthTests` (schema, required-
  missing alerts, optional-missing silence, pre-V2 status,
  stale-build alert firing/suppression, git_sha surfacing,
  Notes mirror prefix). **1121 tests + 41 subtests pass.**

- **Active #16 v2: artifact lineage extension** *(2026-05-17)* --
  closes out the 5 "Defer to v2" follow-ups documented in this
  morning's v1 shipment. The v1 ship stamped lineage on the
  calibration artifacts (OVER + UNDER) and on all four `promote.py`
  audit rows. V2 extends the same `compute_lineage` pattern to the
  five other critical artifact builders so every promotion target
  in the system now carries the full build context (git_sha,
  builder_path, input_hashes, input_dir_summaries, cli_args_summary).

  **Builders stamped**:
  - **Stage-1 cache** (`cache/build_mlb_ou_cache.py`). Inputs
    summarised: `data/games/<season_type>/` directory tree. CLI args:
    season_type, game_types, lines, min_games, max_combined,
    extras_bucket, history window dates, season_weighting_path,
    out path. **Particularly timely**: today's Active #10 shipment
    identified Stage-1 as owning ~100% of the 27pp aggregate
    over-prediction bias, so the next Stage-1 rebuild (Active #8)
    will produce a cache carrying full lineage from day one.
  - **Stage-2 cache** (`cache/build_mlb_stage2_run_env.py`). Inputs:
    Stage-1 cache (hashed) + games dir (summarised). CLI args:
    season_type, game_types, train_end_year, validation_start_year,
    max_total_delta, stage1_cache, out.
  - **Stage-3 v2 weights** (`scripts/analysis/promote_team_offense_v2.py`).
    Inputs: phase4_models.json source artifact (hashed). CLI args:
    source_artifact, output_path, dry_run flag.
  - **EV-policy backtest** (`scripts/analysis/backtest_ev_policy.py`).
    Inputs: training table + manifest (both hashed). Lineage block
    attached to the main report PLUS each of the three runtime-loaded
    model JSONs (`ev_signal_win_if_filled_model`,
    `ev_execution_fill_runtime_model`, `ev_execution_fill_strict_model`).
    CLI args: model_family, artifact_purpose, table_path,
    manifest_path, output_root.
  - **Walk-forward certification** (`scripts/analysis/build_walk_forward_certification.py`).
    Inputs: training table (hashed). CLI args include the readiness
    label + n_filled at build time so the lineage block self-
    describes the cert's state.

  **Real production smoke test on 2026-05-17**: rebuilt
  walk_forward_certification end-to-end; new artifact carries:
  ```
  "lineage": {
    "schema_version": 1,
    "builder_path": "scripts/analysis/build_walk_forward_certification.py",
    "git_sha": "91b70a1348f2",
    "git_dirty": true,
    "input_hashes": {
      "data/analysis_output/training_tables/signal_training_table.jsonl": "sha256:..."
    },
    "cli_args_summary": {
      "readiness_label": "PRELIMINARY", "n_filled": 88, ...
    }
  }
  ```

  **Pattern is identical across all 5 builders**: try/except import
  of `artifact_lineage.compute_lineage` (project-relative import or
  bare module name); call `compute_lineage(builder_path=__file__,
  input_paths=..., input_dir_paths=..., project_root=PROJECT_DIR,
  extra={cli_args_summary: ...})`; attach to top-level `payload["lineage"]`;
  wrap the whole block in `try/except` so a stamp failure never
  blocks the artifact write. Fail-open is critical -- a missing
  lineage stamp is recoverable on the next refresh; a failed
  artifact build is not.

  **What's still deferred to v3**:
  - **Live-engine startup-time lineage logging**: when the runtime
    boots and loads the Stage-1/2/3 caches + calibrator artifacts,
    log a single INFO line per artifact with its lineage summary.
    Operator can grep the runtime log to confirm which artifact
    version was live for any given session. Independent of the
    builders; can ship as a single live_engine_setup.py change.
  - **Cache-freshness daily-review block**: surface "your Stage-1
    cache is N days old, last input mtime was X" in the daily
    review by reading the lineage off each cache file. Becomes
    actionable once a few weeks of cache rebuilds have accumulated.
  - **Historic-artifact backfill**: impossible by construction --
    pre-V1 artifacts were built before lineage existed, so the
    git_sha + dataset state is gone. From today forward, every NEW
    artifact carries lineage.

  **Files**: `cache/build_mlb_ou_cache.py` (Stage-1 stamp in `main`),
  `cache/build_mlb_stage2_run_env.py` (Stage-2 stamp before payload
  write), `scripts/analysis/promote_team_offense_v2.py` (Stage-3 v2
  stamp in `main`), `scripts/analysis/backtest_ev_policy.py`
  (lineage computed once + `_with_lineage` helper attaches to all
  4 artifacts), `scripts/analysis/build_walk_forward_certification.py`
  (cert stamp in `main`). New test module
  `tests/test_lineage_v2_builder_wiring.py` (12 tests: 2 end-to-end
  cert verification, 5 opportunistic on-disk shape checks that
  skip cleanly when artifacts pre-date the shipment, 5 builder
  importability smoke tests). **1093 tests + 41 subtests pass
  (5 skipped on cache artifacts that haven't rebuilt yet).**

- **Active #10: bet-level loss attribution** *(2026-05-17)* -- the
  natural follow-up to today's Active #9 shipment. Where #9
  detects "the model is mis-calibrated by 22pp," this answers
  "which stage of the FV pipeline owns the miscalibration?" The
  load-bearing observation: the FV chain composes logit-additively
  in production, verified to 0.001 on all 87 filled+settled bets
  (calibration is in shadow mode, so calibration_delta ~= 0):
  ```
  fair_value = sigmoid(
      logit(base_fair_value)        # Stage-1
      + stage2_run_env_delta        # Stage-2 (park / weather)
      + team_offense_delta          # Stage-3 (team offense)
      + calibration_delta           # final calibrator
  )
  ```
  That identity gives a clean per-stage probability decomposition
  every operator can read.

  **Math** (`scripts/analysis/build_loss_attribution_report.py`):
  - Per bet: `p0 = base_fv`, `p1 = sigmoid(logit(p0) + s2)`,
    `p2 = sigmoid(logit(p1) + s3)`, `p3 = fair_value`.
  - Per-stage shifts: `s1 = p0 - 0.5` (Stage-1 baseline shift from
    neutral), `s2 = p1 - p0`, `s3 = p2 - p1`,
    `sc = p3 - p2` (calibration residual).
  - Aggregate `bias = mean_p3 - mean_won` (signed; positive =
    over-prediction).
  - Per-stage `mean_shift_in_bias_direction = mean(s_X) * sign(bias)`
    -- the stage's mean shift projected onto the bias direction. A
    stage that pushed FV further into the bias gets a positive
    value; a stage that pushed against the bias gets a negative
    value.
  - `attribution_share = max(0, in_bias_dir) / sum(positive
    in_bias_dirs)` -- "of the stages that hurt, what fraction does
    this one own." Stages that helped (negative) get a 0 share --
    they aren't a culprit.
  - `top_culprits` = stages with share >= 25% sorted DESC. The
    operator's "which lever do I pull" surface.

  **Three time windows** (all / trailing_30d / trailing_7d) so
  the operator can compare recent trends against the full sample.
  **Per-cohort breakdown** across the same 5 dimensions as
  `cohort_calibration_health` (edge_bucket, ask_bucket,
  inning_bucket, line_bucket, current_state_edge_bucket) so the
  operator can drill from "Stage-1 owns 95%" to "Stage-1 owns
  100% of the inning>=8 cohort" if needed.

  **Daily-review block** `_loss_attribution_health` reads the
  trailing-30d aggregate + top culprit and mirrors a single
  retrain-target Notes alert with prefix `Loss-attribution:` when
  `|bias| >= 5pp` AND some stage owns `>= 50%` of the bias. Below
  that, the bias is structurally distributed and the operator
  should read the full artifact rather than act on a Notes-line.
  Stale-artifact check fires after 14d.

  **First production run on 2026-05-17** across 87 filled+settled
  bets (trailing window includes everything since 2026-04-17):
  - Aggregate bias: **+27.1pp** (model over-predicting; mean_p0
    =92.7%, mean_p3=92.7%, mean_won=65.5%).
  - **`stage1_baseline` owns 99.9% of the bias direction
    (mean_shift_in_bias_direction = +42.7pp).**
  - `stage2_run_env`: mean shift +0.04pp (effectively neutral).
  - `stage3_team_offense`: mean shift -0.05pp (actively helping
    by a small amount).
  - `calibration`: 0.00pp (shadow mode -- doesn't shift live FV).

  **The Notes alert it produces:** "trailing-30d aggregate bias
  +27.1pp (model over_predicting, n=87); `stage1_baseline` owns
  100% of the bias direction (shift +42.7pp). This is the retrain
  target -- cross-check with cohort_calibration_health and
  concept_drift_health before changing the live cache."

  **Strategic implication for Active #8**: the roadmap entry for
  "Stage-2 / Stage-3 fresh-test calibration audit (Phase 6)"
  proposed rebuilding both Stage-2 + Stage-3 v2. Today's data
  says **focus the retrain on Stage-1**, not Stage-2 or Stage-3.
  Stage-2 and Stage-3 are doing their job within the noise floor;
  the bot's over-prediction is coming from the Stage-1 Poisson
  cache base rate being too high for the cohorts we're betting
  in. That likely means the Stage-1 cache (5-year historical
  prior) is over-confident on the modern league's run environment,
  or our selection bias is concentrated in states the Stage-1
  cache is most over-confident on. Either way, Active #8's
  rebuild surface narrows from "Stage-2 + Stage-3 retrain" to
  "Stage-1 cache rebuild on fresh seasons + cohort-conditional
  audit."

  **Loosening counterfactuals + execution slippage deferred to
  v2.** v1 ships probability-space attribution only. Execution
  slippage (`actual_fill_price - decision_ask`) is a separate
  decomposition that affects ROI but not FV; it requires
  `actual_fill_price` in the training table (currently missing
  for the 87-bet historical sample). v2 adds it.

  **Files**: new `scripts/analysis/build_loss_attribution_report.py`
  (~480 LOC: logit/sigmoid math, `BetDecomposition` dataclass,
  `decompose_bet` projection + filter, `aggregate_decompositions`
  math with positive-sum normalization, 3-window slicing,
  per-cohort aggregation across the 5 standard dimensions, markdown
  render), `scripts/analysis/run_daily_refresh.py` (new
  `loss_attribution_report` step after walk_forward_certification),
  `scripts/analysis/build_daily_human_review_report.py` (new
  `_loss_attribution_health` block + Notes mirror + build_report
  wiring), 32 new tests in `test_build_loss_attribution_report.py`
  (math identities, filter logic, stage-shift math, aggregate +
  attribution-share math, window slicing, cohort breakdown,
  schema completeness, markdown render, end-to-end main + empty
  input), 10 new tests in `LossAttributionHealthTests` (artifact
  loading, clear-culprit alert text, no-single-culprit softer
  alert, small-bias suppression, under-predict direction tag,
  empty trailing window, stale artifact, compact schema, Notes
  mirror prefix). **1086 tests + 41 subtests pass.**

- **Active #9: per-cohort calibration drift detection** *(2026-05-17)* --
  the 8th drift dimension. The aggregate `calibration_health` block
  scores the calibrator's method selection + audit metadata; this
  block answers the orthogonal question: **does the calibrated FV
  actually match realized win-rate, per cohort?** Today's zero-bet
  audit was the catalyst: the aggregate calibration looked green
  while a 22pp aggregate reliability gap was hiding in plain sight
  beneath it, and the CHC@CWS over-the-line miss (model said 54.7%,
  reality went 17 runs vs 10.5 line) was the canonical failure mode.

  **What it computes** (in `build_daily_human_review_report.py`):
  - For every filled+settled bet with `won` populated and
    `fair_value` in [0, 1]: `_aggregate_calibration` returns `n`,
    `mean_fair_value`, `mean_won`, `reliability_gap` (=
    `|mean_fv - mean_won|`), and `brier` (= `mean((fv - won)^2)`).
  - Aggregate metrics computed once on the trailing-7d window
    (same window as `cohort_roi_health` so cross-comparison stays
    apples-to-apples).
  - Per-cohort metrics computed across the same 5 dimensions as
    `cohort_roi_health` via `COHORT_DIMENSIONS`: `edge_bucket`,
    `ask_bucket`, `inning_bucket`, `line_bucket`,
    `current_state_edge_bucket`. Each bucket also carries
    `reliability_gap_ratio_vs_aggregate` for ad-hoc inspection.

  **Two alert classes** ship together. The user-spec was cohort-only
  but real-data smoke testing showed cohorts rarely reach the
  required n=30 at current volume; without an aggregate alert the
  block would have stayed silent today even with a 22pp aggregate
  gap. Both alerts mirror to top-level Notes with prefix
  `Cohort-calibration:`.
  - **Aggregate alert**: fires when aggregate reliability gap >= 10pp
    AND aggregate n >= 15. Catches whole-model miscalibration even
    when no single cohort dominates. The first production run hit
    this at **22.1pp gap (n=44)**, exact same shape today's
    zero-bet audit predicted.
  - **Per-cohort vs aggregate ratio**: fires when a cohort's
    reliability gap is >= 2x the aggregate gap AND cohort n >= 30
    AND aggregate gap >= 1pp (the floor avoids dividing by ~0).
    Suppresses today's failure-mode noise while letting structural
    cohort divergence surface once volume accumulates.

  **Direction-aware alert text** spells out whether the model is
  `over-predicting` or `under-predicting` per cohort. Reuses the
  same promotion / demotion / concept-drift attribution helpers as
  `cohort_roi_health`, so alerts get the same enrichment suffixes
  (e.g. today's aggregate alert auto-attached
  `[concept-drift: stage2_run_env_delta psi 2.32,
  base_fair_value psi 1.75, team_offense_delta psi 1.37]`).

  **Why this is the 8th drift dimension, not a tweak to the
  existing `calibration_health` block**: the existing block scores
  the calibrator's *selection* metadata (which method, stability
  gate, identity rejection). This block scores the calibrator's
  *output quality* against realized outcomes. They're orthogonal
  -- the calibrator can pick the "right" method by validation
  logloss and still produce systematically biased FVs if the
  underlying Stage-1/2/3 inputs have drifted (which is exactly
  the situation `concept_drift_health` has been flagging for ~5
  days). Splitting them keeps each block's responsibility tight
  and avoids blowing up the existing calibration audit schema.

  **First production run on 2026-05-17** (44 filled+settled bets
  in the trailing 7d window):
  - Aggregate alert fires: `mean_fv 92.6% vs mean_won 70.5%`,
    gap **22.1pp**, model **over-predicting systematically**.
    Attribution suffix: `stage2_run_env_delta psi 2.32`,
    `base_fair_value psi 1.75`, `team_offense_delta psi 1.37`
    -- consistent with the concept-drift block's 5-day-old finding
    that the model is being fit on materially-shifted inputs.
  - No per-cohort alerts fire (all buckets have n < 30 at current
    volume). The block will start firing cohort-level alerts as
    volume accumulates; today the aggregate alert IS the signal.
  - Cohort-level table still populated for operator inspection:
    `inning_bucket=6` shows 42pp gap on 13 bets, `ask_bucket=0.75-0.85`
    shows 35pp gap on 29 bets -- both directionally consistent with
    the aggregate but below the n=30 alert floor.

  **Files**: `scripts/analysis/build_daily_human_review_report.py`
  (5 new module constants `COHORT_CALIBRATION_*`, new
  `_bet_is_calibratable` filter helper, new `_aggregate_calibration`
  math helper, new `_cohort_calibration_health` block computing
  aggregate + per-cohort + ratio + alerts + attribution,
  `build_report` wiring, `_build_notes` mirror with
  `Cohort-calibration:` prefix). 21 new tests in
  `CohortCalibrationHealthTests` covering the filter, the math
  (perfectly-calibrated zero gap, over- and under-prediction
  directions, empty cohort returns None), alert firing /
  suppression boundaries (aggregate threshold, aggregate min-n,
  per-cohort 2x ratio, per-cohort n=30 floor, missing-bucket
  exclusion, aggregate gap floor for ratio test), trailing
  reviews contributing to window, schema completeness,
  promotion-attribution suffix, and Notes-mirror prefix.
  **1044 tests + 41 subtests pass.**

- **Active #11: counterfactual gate-change logger** *(2026-05-17)* --
  the second-fastest gate-decision tool in the project, sitting next
  to the walk-forward certification (Active #1) but answering a
  different question. Cert says "is this gate structurally sound on
  average against READY-sized data?"; counterfactual says "if I had
  tightened this gate by one click over the trailing-30d / 7d, how
  much money would I have saved in realized P&L?"

  **Why offline + reuse the cert's gate library** instead of the
  per-candidate runtime ledger the roadmap text proposed:
  `signal_training_table.jsonl` already carries every field needed
  (edge, ask, inning, runs_needed, base_fair_value, stage2 delta,
  current_total, lead_abs, etc.) joined to target_win / target_profit
  per filled bet. `build_walk_forward_certification.py` already
  defines `GATE_DEFS` + `_sweep_one` that re-evaluate any gate at
  any threshold against any cohort. The counterfactual reuses both
  -- zero runtime risk, single source of truth for gate definitions,
  and the report ships richer cross-tabs than a per-candidate ledger
  could surface.

  **What ships**:
  - **`scripts/analysis/build_gate_counterfactual_report.py`** (~410
    LOC). Imports `GATE_DEFS`, `BetRow`, `load_bet_rows`, `_sweep_one`
    from the cert. For each gate × each sweep threshold × each window
    (`all`, `trailing_30d`, `trailing_7d`) computes:
    - `counterfactual_profit_delta_vs_current` = the realized $ that
      enforcing the alt threshold instead of the current one would
      have saved (positive) or sacrificed (negative). Formula:
      `cur_blocked.total_profit - alt_blocked.total_profit` (the
      identity works for both tightenings and loosenings; sign
      carries the interpretation).
    - `kept_roi_delta_vs_current` = kept_cohort_roi shift after the
      flip.
    - Anchor row at the current threshold (`is_current=True`,
      delta=None) so every panel has a baseline anchor.
    - `is_tightening` direction inference (max-direction lower = tighter,
      min-direction higher = tighter).
  - **`top_recommendations`** list ranks the highest-impact tightening
    counterfactuals over trailing-30d (primary) and trailing-7d
    (freshest signal, lower confidence). Filters: tightening-only,
    blocked_n_filled >= 5, $-savings >= $25 (builder floor),
    sorted DESC by $-savings, cap at 10 entries. Confidence label
    auto-degrades on blocked-N: high >= 20, medium >= 10, low < 10.
  - **`gate_counterfactual_report` refresh step** wired in
    `run_daily_refresh.py` right after `walk_forward_certification`
    (depends on the same training table). Outputs to
    `data/analysis_output/gate_counterfactual/gate_counterfactual_report.{json,md}`.
  - **`gate_counterfactual_health` block** in
    `build_daily_human_review_report.py` reads the artifact,
    compacts the top recommendations onto the daily review payload,
    and mirrors the top-3 high-impact ones (>= $40 savings, the
    Notes-mirror layer applies a stricter floor than the builder
    so day-to-day single-bet noise doesn't crowd Notes) to top-level
    `notes` with prefix `Gate-counterfactual:`. Stale-artifact age
    check fires after 14d.

  **Loosening counterfactuals deferred to v2.** When the current
  threshold blocks a bet that WOULD have won, the cert sweep gives
  the right number, but the "would have placed AND filled" assumption
  on never-placed candidates is unsupported without a p_fill model.
  Tightening is high-confidence (we know exactly which bets were
  placed and filled); loosening is lower-confidence. v1 surfaces
  tightening only; v2 can add loosening with an explicit p_fill
  estimator + lower confidence labeling.

  **First production run on 2026-05-17** across 178 settled bet rows:
  7 tightening recommendations cleared the trailing-30d floor:
  - `gate_min_entry_ask` 0.55 → 0.65 saves **$+75.64** (blocked N=15,
    blocked ROI -36.0%, confidence medium).
  - `gate_max_base_fv` 0.99 → 0.95 saves **$+54.12** (blocked N=41,
    blocked ROI -18.6%, confidence high).
  - `gate_min_current_total` 4 → 5 saves **$+44.31** (blocked N=20,
    blocked ROI -20.2%, confidence high).
  - `gate_s2_suppress_max` -0.20 → 0.0 saves **$+41.34** (blocked N=23,
    confidence high).
  - `gate_runs_needed_max` 3.5 → 2.5 saves **$+35.54** (blocked N=13,
    confidence medium).
  - Plus 2 more (gate_min_inning, gate_min_edge) below the Notes-
    mirror floor but visible in the JSON artifact.

  Trailing-7d (single recommendation surfacing the freshest signal):
  `gate_max_base_fv` 0.99 → 0.95 saves **$+28.66** in just the
  trailing 7d (blocked N=24) -- this is the single-gate flip with
  the clearest immediate evidence, and the cert's gate scorecard
  expansion shipped earlier today is consistent (gate_max_base_fv
  has historically been at saturation; lowering it captures the
  phantom-score fingerprint earlier).

  **Operational use**: read the trailing-30d top section first
  (medium/high confidence), cross-check against the cert's per-gate
  verdict (currently PRELIMINARY at 178 bets; doesn't actuate yet
  but informs operator's eventual day-30 threshold decisions), then
  use `promote.py gate-threshold <name> <value>` to change. Daemon
  is preview-only for gate-threshold; no auto-actuation.

  **Files**: new `scripts/analysis/build_gate_counterfactual_report.py`,
  `scripts/analysis/run_daily_refresh.py` (new
  `gate_counterfactual_report` step), `scripts/analysis/build_daily_human_review_report.py`
  (new `_gate_counterfactual_health` block + Notes mirror +
  build_report wiring), new `tests/test_build_gate_counterfactual_report.py`
  (25 tests covering window slicing, tightening direction inference,
  counterfactual math, applicability, top_recommendations filters,
  confidence labels, markdown render, end-to-end main), new
  `GateCounterfactualHealthTests` (10 tests in the daily-review test
  file). 1023 tests + 41 subtests pass.

- **Walk-forward gate-scorecard expansion** *(2026-05-17)* -- the
  Active #1 walk-forward certification report expanded from 5 to
  **15 gates** evaluated per refresh. When the cert hits READY
  (~2026-06-10), every enforced trading gate has its own verdict
  + threshold sweep, not just five.

  **Previous coverage** (5 gates): `gate_extreme_edge`,
  `gate_min_edge`, `gate_min_inning`, `gate_min_entry_ask`,
  `gate_runs_needed_max`. Most never blocked any bet in the
  sample, so 4 of 5 returned `KEEP (low confidence) -- 0 blocked`
  with no actionable signal.

  **Added coverage** (10 new gates):
  - **`gate_max_base_fv`** (universal) -- blocks when Stage-1
    base_fair_value > 0.99; phantom-score fingerprint.
  - **`gate_fv_ask_gap_max`** (inning>=7) -- the gate whose
    default we lowered 0.28 -> 0.26 today; now its impact is
    auditable cohort-by-cohort.
  - **`gate_min_current_total`** (universal) -- low-scoring-
    game block (away+home < 4).
  - **`gate_inn5_rn_max`** (inning==5) -- reliever-transition
    runs-needed cap.
  - **`gate_inn6_rn_max`** (inning==6) -- setup-reliever dead-
    zone runs-needed cap.
  - **`gate_close_game_rn`** (lead_abs<2) -- close-game
    runs-needed cap.
  - **`gate_s2_suppress_max`** (inning>=6) -- Stage-2 run-env
    suppression. Direction='min' (block when delta is BELOW the
    threshold; more negative = block).
  - **`gate_high_line_min_edge`** (line>=8.5) -- separate edge
    floor for high-line markets.
  - **`gate_high_line_min_inning`** (line>=8.5) -- separate
    inning floor for high-line markets.
  - **`shadow_gate_current_state_edge_min`** -- shadow-only;
    no production threshold. The 2026-05-17 cohort breakdown
    showed `cse<0.03` is +10.5% ROI vs `cse>=0.08` -11.4%, so
    Active #3's proposed `current_state_edge_min >= 0.05` is
    being re-evaluated. This shadow gate runs the sweep so the
    operator can read the EXPLORE verdict and decide a
    direction.

  **Mechanism**: `GateDef` gained an `applicability` predicate
  that filters the bet population BEFORE threshold evaluation.
  Composite gates (e.g. `gate_inn6_rn_max` applies only when
  inning==6) exclude out-of-domain rows from BOTH kept and
  blocked cohorts so the verdict comparison stays apples-to-
  apples within the gate's scope. `BetRow` gained four fields
  (`current_total`, `lead_abs`, `base_fair_value`,
  `stage2_run_env_delta`) so the composite gates have the
  signals they need. `evaluate_gate` gained a `shadow_only`
  branch that emits an EXPLORE verdict (best sweep threshold by
  kept-vs-blocked ROI delta) instead of KEEP/RETUNE/RETIRE.

  **Findings on 2026-05-17 PRELIMINARY data** (88 fills / 27
  dates; verdicts directional only):
  - `gate_extreme_edge` remains the only confidently-supported
    gate (blocked cohort -69% ROI vs kept +4.8%; 73.8pp gap).
  - `gate_fv_ask_gap_max` (inning>=7 cohort) shows 28 kept at
    +16.9% ROI vs 2 blocked at -100%. Gate is doing real work
    within its domain.
  - `gate_inn6_rn_max` (inning==6 cohort) shows 25 kept bets
    at **-36.8% ROI** with 0 blocked. **Inning 6 is the worst-
    performing band in the entire dataset**; current rn>=2.5
    threshold blocks nothing. Strong candidate for a TIGHTER
    threshold once data matures.
  - `gate_close_game_rn` (close-game cohort) shows 25 kept at
    +14.5% ROI with 0 blocked. Close games look profitable in
    this sample -- counter-intuitive.
  - `shadow_gate_current_state_edge_min` confirms the inverted
    signal we expected: the operator should re-think Active #3's
    proposed direction.

  **Files**: `scripts/analysis/build_walk_forward_certification.py`
  (`BetRow` extension, `GateDef.applicability` + `shadow_only`
  fields, `_sweep_one` applicability filter, `evaluate_gate`
  shadow branch, 10 new `GateDef` entries),
  `tests/test_build_walk_forward_certification.py` (11 new tests:
  4 applicability semantics, 2 shadow-only EXPLORE, 2 BetRow
  extension, 3 gate-presence + universal-vs-composite shape).
  988 tests + 41 subtests pass.

  **Refresh**: the daily refresh already runs the cert; tomorrow's
  refresh will produce the expanded scorecard automatically. No
  wiring change.

- **CLI default convergence** *(2026-05-17)* -- five `real_trader.py`
  defaults bumped to match values the operator has been running
  explicitly for weeks, removing the need to pass them on every
  invocation. Each change is supported by either evidence in the
  code path or by alignment with newer subsystems (Phase C C2
  inventory cap, Phase D queue-position research, settlement-
  lifecycle robustness).
  - `DEFAULT_PER_GAME_BUDGET_FRACTION`: **0.35 -> 0.40**. Aligns
    with the Phase C C2 inventory cap (max 50 shares/game ~= $50
    at typical ask vs $100 daily budget = 50% ceiling); 40%
    leaves clean headroom.
  - `DEFAULT_FV_ASK_GAP_MAX`: **0.28 -> 0.26**. Risk-reducing
    direction (blocks more late-game phantom-score patterns where
    fair_value - decision_ask exceeds the cap in inning >= 7).
    TR13 already lowered 0.30 -> 0.28 after a PIT@TEX loss;
    0.26 is the next conservative step. Active #1 walk-forward
    should re-certify at day-30 trigger; the change can only
    REDUCE exposure, never increase it.
  - `DEFAULT_CAPTURE_DURATION`: **30.0s -> 120.0s**. The
    post-signal book-capture analysis
    (`scripts/analysis/analyze_book_captures.py`) uses
    `max_elapsed=60.0` for fill-window simulation, so the
    previous 30s default truncated that analysis silently.
    120s covers all current analysis + headroom for Phase D
    queue-position research without recapture.
  - `DEFAULT_CAPTURE_DEPTH`: **3 -> 5**. No current analysis
    traverses past top-of-book, but Phase D market-maker work
    needs queue-position data (multi-level depth). Cost: 2
    extra rows per snapshot -- trivial vs data-utility upside.
  - `--wait-for-clob`: **False -> True** (via
    `argparse.BooleanOptionalAction` so `--no-wait-for-clob`
    becomes the explicit opt-out). Pure operational
    robustness; survives scheduled CLOB downtime windows
    during startup with no impact on trading behavior. False
    was a debug-iteration convenience; True is the production-
    correct default.

  After this change, the operator's invocation collapses from:
  ```
  python scripts/trading/real_trader.py \
     --stake-mode flat --stake 10 --daily-budget 100 \
     --per-game-budget-fraction 0.40 --max-open-orders 7 \
     --fv-ask-gap-max 0.26 --capture-duration 120 \
     --capture-depth 5 --ev-policy-mode shadow \
     --wait-for-clob --performance-mode
  ```
  to:
  ```
  python scripts/trading/real_trader.py \
     --stake-mode flat --stake 10 --daily-budget 100 \
     --max-open-orders 7 --ev-policy-mode shadow \
     --performance-mode
  ```
  All 977 tests + 41 subtests pass (no test broke; the one
  characterization fixture that pins `fv_ask_gap_max=0.28`
  remains intentional for golden testing).

- **Active #16: model lineage tracking** *(2026-05-17)* --
  completes the Phase C v2 safety triangle alongside #12 (detect)
  and #13 (react). When a fast Wilson-UB demote fires on a
  failing promotion, the operator's first question is "which
  artifact + which git_sha?" Without lineage, that needs git-log
  archaeology against approximate timestamps. With lineage, the
  artifact carries its own answer.

  **Core module** `scripts/analysis/artifact_lineage.py`:
  - `compute_lineage(builder_path, input_paths, input_dir_paths,
    project_root, extra)` returns:
    ```
    {
      schema_version, built_at_utc, builder_path,
      git_sha, git_branch, git_dirty,
      input_hashes: {path: sha256:...},      # files small enough to hash
      input_dir_summaries: {path: {n_files, max_mtime, min_mtime}},
      python_version,
      <extras>
    }
    ```
  - `promotion_lineage()` returns a trimmed dict stamped at
    promote time (current git_sha + timestamp) so the audit row
    knows both "what was built" and "when/where it was promoted."
  - Hashes are sha256 truncated to 16 hex chars (64 bits of
    entropy, more than enough for change detection); audit rows
    stay compact.
  - Best-effort throughout: missing git, unreadable files, or
    non-existent paths all return None. The lineage stamp must
    NEVER block an artifact build.

  **Build-time stamping** (`calibrate_signal_probabilities.py`):
  both OVER and UNDER calibration artifacts now carry
  `lineage` at the top level of the JSON. Real production run
  on 2026-05-17 produced both with same git_sha (`0840bb7c1cac`),
  different `cli_args_summary.side` discriminator, full input
  file hashes.

  **Promote-time stamping** (`promote.py`):
  - `PromotionEvent` dataclass gains `source_artifact_lineage`
    + `promotion_lineage` (both default None for back-compat
    reads of legacy audit rows).
  - All four success-path PromotionEvent constructors
    (stage2 / stage3-v2 / stake-scaling / gate-threshold)
    populate both fields via `_capture_artifact_lineage()` +
    `_compute_promotion_lineage()` helpers.
  - `promote.py status` surfaces lineage per lever:
    `artifact lineage: built_at=... by=... git_sha=abc123(dirty)`
    `promoted from: git_sha=xyz789 branch=main`

  **Defer to v2** (clearly documented, same `compute_lineage`
  pattern for each):
  - Stage-2 cache builder (`cache/build_mlb_stage2_run_env.py`)
  - Stage-3 v2 weights (`promote_team_offense_v2.py`)
  - EV-policy artifacts (`backtest_ev_policy.py`)
  - Walk-forward summaries
  - Live-engine startup-time lineage logging
  - Backfilling historic artifacts is impossible -- they were
    built before lineage existed and the sha/dataset state is
    gone. From today forward, every NEW promotion is traceable.

  **Real value scenario**: when fast_demote fires on stake-scaling
  tomorrow, the operator runs `promote.py status` and sees:
  `artifact lineage: built_at=2026-05-17T15:30Z by=analyze_stake_scaling_promotion.py git_sha=0840bb7c1cac`
  `promoted from: git_sha=abc789ef branch=main`
  Instantly answers "what was built, when, by what code, from
  what working tree." No git-blame required.

  **Files**: new `scripts/analysis/artifact_lineage.py` (~265 LOC),
  `scripts/analysis/promote.py` (lineage capture helpers + 2
  new `PromotionEvent` fields + status surface + threaded into
  4 success-path event writes),
  `scripts/analysis/calibrate_signal_probabilities.py` (stamps
  lineage on both OVER + UNDER artifacts).
  2 new test classes (`test_artifact_lineage.py` 22 tests +
  `PromotionLineageWiringTests` 1 end-to-end). 974 tests +
  41 subtests pass.

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

8. **Stage-1 Alt-A promotion (primary Active #8 path, as of 2026-05-17
   loss attribution).** Loss attribution identified Stage-1 as owning
   ~100% of the 27pp aggregate over-prediction bias; the cell-conditional
   drill narrowed the fix to "use empirical-when-available instead of
   Poisson smoothing"; the shadow-override report quantified -6pp
   bias reduction; the runtime now logs Alt-A alongside production on
   every candidate; `promote.py stage1` provides the atomic swap;
   **2026-05-18: the Alt-A staging cache itself ships** -- builder
   flag + refresh step + daily-review surface, with 4,200/4,298 cells
   overridden and mean signed delta -2.74pp on the first live build.
   The ENFORCE flip is now one CLI command after paper-mode
   validation clears its bar: `python scripts/analysis/promote.py
   stage1 --source cache/mlb_ou_cache_alt_a.staging.json`. Bar to
   clear: stable Stage-1 Alt-A staging cache across the full
   paper-mode runway + runtime Alt-A shadow shows continued bias
   reduction in real per-bet outcomes (not just offline replay).
   - Files: `cache/build_mlb_ou_cache.py` (Alt-A builder, shipped),
     `scripts/analysis/promote.py` (stage1 subcommand, shipped),
     `scripts/analysis/build_daily_human_review_report.py`
     (staging-health block, shipped).

   **Original Stage-2 / Stage-3 audit text below preserved** -- still
   open but deprioritized now that Stage-1 is the proven bias owner.
   Rebuild Stage-3 v2 + Stage-2 (density_alt, hr_factor) on
   2026-05-07 onward once ~30 days of post-TR20 games are in the
   corpus, validate against a held-out slice, and confirm the family
   weights chosen by the validation tuner haven't drifted.
   - Files: `cache/build_mlb_stage2_run_env.py`, `model_improvements/`
     Phase 6 notes (to be written), training corpora under `data/games/`.

9. **Per-cohort calibration drift detection.** *Shipped 2026-05-17.*
   See the Recently Completed "Active #9: per-cohort calibration
   drift detection" entry for full details. v1 ships two alert
   classes: an aggregate-level alert (whole-model gap >= 10pp,
   n >= 15) and the originally-specced per-cohort vs aggregate
   ratio alert (cohort gap >= 2x aggregate, n >= 30). The
   aggregate alert was added during real-data smoke testing when
   the original cohort-only design would have stayed silent
   despite a 22pp aggregate reliability gap visible in today's
   production data. Mirrors `cohort_roi_health` decomposition
   across 5 dimensions and reuses promotion / demotion / concept-
   drift attribution helpers. Original roadmap text below
   preserved for context.
   - Files: `scripts/analysis/build_daily_human_review_report.py`
     (new `cohort_calibration_health` block);
     `calibrate_signal_probabilities.py` (per-cohort reliability
     decomposition helper).

10. **Bet-level loss attribution.** *Shipped 2026-05-17.* See the
    Recently Completed "Active #10: bet-level loss attribution"
    entry for full details. v1 ships probability-space attribution
    via the logit-additive FV chain identity (verified to 0.001 on
    all 87 production bets). First production run reveals Stage-1
    owns ~100% of the 27pp aggregate over-prediction bias, with
    Stage-2/Stage-3 actively neutral-to-helping. This re-targets
    Active #8 from "rebuild Stage-2 + Stage-3" to "rebuild Stage-1
    cache." Execution slippage deferred to v2 (training table
    doesn't yet carry `actual_fill_price` for historic rows).
    - Files: new
      `scripts/analysis/build_loss_attribution_report.py`; consumed
      by `build_daily_human_review_report.py` (new
      `loss_attribution_health` block).

11. **Counterfactual gate-change logger.** *Shipped 2026-05-17.* See
    the Recently Completed "Active #11: counterfactual gate-change
    logger" entry for full details. v1 ships as pure offline analysis
    that reuses the cert's `GATE_DEFS` + `_sweep_one` against three
    time windows (all / trailing_30d / trailing_7d), with a
    `top_recommendations` list ranked by trailing-30d realized-$
    saved and a daily-review block that mirrors high-impact
    tightenings (>= $40 savings) to Notes. Tightening direction only
    in v1; loosening counterfactuals deferred to v2 (need a p_fill
    model on never-placed candidates). First production run on 178
    bets surfaced 7 actionable tightenings, the headline being
    `gate_min_entry_ask` 0.55->0.65 saving $75.64 over the last 30d.
    Original roadmap text below preserved for v2-design context.
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

17. **Scoped Alt-A enforce (cohort-aware empirical override).**
    *2026-05-19 audit follow-up.* The 2026-05-19 FV audit
    confirmed Poisson over-predicts by 4-7pp at high FV in the
    Stage-1 cache across every line. A naive global
    `--smoothing-mode empirical_when_available` flip of the
    production builder would close most of the gap (mean signed
    delta -2.7pp in probability units across 4200/4298 overridden
    cells) BUT the existing shadow-override report shows
    **-23.8pp regression on the `inning>=8` cohort** (n=7, noisy
    but directionally clear). Operationally correct path is
    scoped application: apply empirical-when-available only on
    cohorts where the shadow report shows durable improvement;
    keep Poisson on cohorts where it regresses.
    - Design surface (open): a new per-cell or per-cohort
      `smoothing_mode_decision` block in
      `cache/build_mlb_ou_cache.py`'s Alt-A pass that reads the
      latest `build_stage1_shadow_override_report.py`
      `cohort_breakdown` artifact and overrides per-cell based
      on the cohort the cell belongs to. Alternatively, runtime
      gating in `signal_pipeline_gates_post_fv.py` that switches
      `inferred_state_base_source` per candidate.
    - Pre-req: the shadow-override report's per-cohort
      breakdown is already shipped (2026-05-19). What's
      needed is the consumer that turns its verdict into the
      cell-or-runtime override decision.
    - Files: `cache/build_mlb_ou_cache.py`,
      `scripts/analysis/build_stage1_shadow_override_report.py`
      (read), runtime path in `signal_engine.py` or
      `signal_pipeline_gates_post_fv.py`.
    - Until shipped, the 2026-05-19 band-gated calibrator
      enforce (see Recently Completed) catches the high-FV
      overconfidence band downstream.
    - **Boundary-empirical guard (2026-05-20 audit follow-up,
      already in place across all paths)**: empirical rates
      exactly equal to 0 or 1 (degenerate sample artifacts;
      logit blows up) must NEVER be promoted over Poisson. The
      build pass at `cache/build_mlb_ou_cache.py:678`, the
      runtime shadow at
      `scripts/trading/signal_pipeline_gates_post_fv.py:311`,
      and the offline report at
      `scripts/analysis/build_stage1_shadow_override_report.py:215`
      all gate on `0.0 < emp < 1.0`. The 2026-05-19 session
      surfaced 3 of 9 bets with `inferred_state_base_empirical
      = 1.000` at n=130-162 -- these are real samples but
      sit on a boundary the chain can't consume. Per-line
      boundary-skip counter added to the Alt-A summary
      (`line_boundary_skips`) so the staging-health block can
      surface how often the guard fires per line. Scoped Alt-A
      design must preserve this guard: cohorts where empirical
      is reliably on the boundary should keep Poisson, not
      get a "use empirical" override.

14. **Backup retention policy + PSI-history GC.** *Shipped
    2026-05-17.* See the Recently Completed "Active #14: backup
    retention + PSI-history GC" entry for full details. Final
    design: keep `.prior_promote.json` as the single "latest
    backup" (demote contract unchanged), rotate the prior backup
    into a sibling `.prior_promote_archive/` directory with a
    timestamped filename BEFORE the new write, GC the archive to
    BACKUP_ARCHIVE_KEEP=5 most recent. PSI history trimmed to
    PSI_HISTORY_RETENTION_DAYS=365 on every append, anchored on
    the LATEST date in the file (not today's wall clock) so the
    trim is stable on stale corpus runs.
    - Files: `scripts/analysis/promote.py` (archive + GC helpers),
      `scripts/analysis/build_concept_drift_report.py` (psi_history
      trim on append).

15. **Promotion-lag tracker + operator doc.** *Shipped 2026-05-19.*
    See the Recently Completed "Active #15: promotion-lag tracker +
    operator doc" entry for full details. Both pieces shipped:
    (a) `docs/operational/promotion_lag.md` explains
    promote-time vs effect-time per lever + related daily-review
    blocks + what the tracker doesn't catch; (b) new
    `_promotion_lag_health` block in
    `build_daily_human_review_report.py` returns 5-status per-
    lever verdicts (`effective_in_runtime` / `pending_next_
    session_boot` / `cache_missing` / `no_session_history` /
    `check_error`), alerts when a lever is pending for >24h,
    mirrors via Notes prefix `Promotion-lag:`. Engine-boot proxy:
    first-bet `placed_at` of latest session file across paper +
    live trading roots (with fall-back to session `generated_at`
    for zero-bet sessions). First production run: 0 alerts, all 5
    lever verdicts correct (stage1 pending 0.32h post-refresh,
    stage2 effective 240h, the other 3 `cache_missing` --
    expected first-time state).
    - Files: `scripts/analysis/build_daily_human_review_report.py`
      (new `_promotion_lag_health` block + helpers);
      `docs/operational/promotion_lag.md` (new one-page operator
      doc).

16. **Model lineage tracking.** *Shipped 2026-05-17.* See the
    Recently Completed "Active #16: model lineage tracking" entry
    for full details. Both calibration artifacts (OVER + UNDER)
    now carry build-time `lineage: {git_sha, git_branch,
    git_dirty, builder_path, input_hashes, input_dir_summaries,
    built_at_utc, python_version}`. The four `promote.py`
    handlers stamp BOTH `source_artifact_lineage` (pulled from
    the artifact's own lineage block) AND `promotion_lineage`
    (fresh git sha at promotion time) on every success-path
    audit row. Surfaced in `promote.py status` so operators can
    answer "which artifact + which git_sha?" without git
    archaeology when fast_demote (#13) fires. **V2 shipped
    2026-05-17 (same day)**: lineage now also stamped on the
    Stage-1 cache, Stage-2 cache, Stage-3 v2 weights, EV-policy
    artifacts (report + 3 model JSONs), and walk-forward
    certification report. **V3 shipped same day**: lineage
    visibility via startup-time INFO log per cache load + new
    `cache_lineage_freshness_health` daily-review block.
    **V4 shipped same day**: cross-artifact consistency check
    via new `compare_input_hash` helper + new
    `_cross_artifact_consistency_health` daily-review block
    that surfaces per-artifact stale + cross-artifact divergence
    alerts. Remaining v5 follow-up: lineage-aware demote UX
    + transitive consistency (walk the dependency graph rather
    than direct-input matches only).

18. **Line-5.5 high-FV slice guard.** *2026-05-19 audit
    follow-up.* Per the audit's per-line slice (1,376 settled
    OVER predictions), at raw FV >= 0.90 the realized hit rate
    for line=5.5 is **51% on n=92** vs claimed 96% -- the worst
    miss in the dataset, and the calibrator only partially
    rescues it. Even with band-gated enforce the residual EV is
    poor. Options: (a) a hard per-line FV ceiling at 5.5 (e.g.
    no bets at line=5.5 when raw_fv >= 0.90), (b) a per-line
    stake dampener, (c) a separate Stage-1 build pass for the
    extra-low-lines bucket. (a) is cheapest. Audit input data:
    `data/analysis_output/calibration/signal_win_calibration_predictions.jsonl`.
    Files: `scripts/trading/signal_pipeline_gates_post_fv.py`,
    `signal_config.py` for the gate threshold.

19. **Mid-band [0.80,0.90) calibrator under-confidence
    refit.** *2026-05-19 audit follow-up.* The 2026-05-19
    audit found the Platt calibrator pulls too aggressively
    in [0.80,0.90) -- raw is +5-9pp overconfident vs realized
    but calibrated is 10-16pp **under** realized. The
    band-gated enforce ships shipped 2026-05-19 works around
    this by NOT applying the calibrator in this band, but the
    correct long-term fix is a per-line refit (Platt currently
    fits a global slope+intercept; per-line + per-family would
    let the [0.80,0.90) band reach a tighter local fit). Or
    swap to isotonic which already wins on the no_score_drift
    family. Files:
    `scripts/analysis/calibrate_signal_probabilities.py`.

20. **Replace Poisson tail with negative-binomial fit.**
    *2026-05-19 audit follow-up.* The Stage-1 cache uses
    Poisson smoothing which is structurally too thin-tailed
    for run-scoring (offenses go cold for stretches; the
    Poisson distribution under-models the fat left tail of
    1-0-1-0 inning sequences). Audit shows poisson > empirical
    by 4-7pp at every line where poisson >= 0.85 across 845-
    1919 well-supported cells per line -- the bias is
    structural, not sampling noise. Replacing with a
    Negative-Binomial fit per phase lambda or per cell (where
    n_samples adequate) would shorten the tail. Bigger model
    change than the cheap wins; ship after Active #17 (Scoped
    Alt-A) demonstrates the cohort-aware promotion machinery
    works. Files: `cache/build_mlb_ou_cache.py`.

21. **Chain-rebuild stale-input artifacts.**
    *2026-05-20 audit follow-up.* The 2026-05-20 audit found
    7 cross_artifact_consistency_health alerts firing every
    refresh. Today's reorder (`concept_drift_report` now
    runs BEFORE `calibrate_signal_probabilities`) eliminates
    2 of them. The remaining 5 are downstream-of-
    `signal_training_table.jsonl`: `walk_forward_cert`,
    `ev_policy_report`, and `stage1_cell_loss_attribution`
    all record the table's hash at their build moment, but
    something modifies the table later in the refresh
    (mtime sits ~22 min after walk_forward_cert finishes;
    no refresh step OBVIOUSLY writes the file after build,
    so root cause needs investigation -- possible
    parallelism, possible side-effect from a step claiming
    "read-only"). Two design directions:
    (a) **Investigate-and-reorder** (cheaper): figure out
        which step is updating `signal_training_table.jsonl`
        after the build step and either ban the side-effect
        or move consumers AFTER it.
    (b) **Auto-chain-rebuild** (general fix): at end of
        refresh, recompute cross-artifact consistency in
        memory; for each stale artifact, look up its
        rebuilder from a new
        `ARTIFACT_LABEL_TO_REFRESH_STEP` mapping and re-run
        it; cap iterations at 2 to prevent runaway. Surfaces
        a `chain_rebuild_iterations` counter in the refresh
        audit so operators see how often the fix-up runs.
    Until shipped, the daily review's
    `cross_artifact_consistency_health` still surfaces every
    stale artifact for manual rebuilds. Files:
    `scripts/analysis/run_daily_refresh.py`,
    `scripts/analysis/human_review/system_health.py`
    (`_cross_artifact_consistency_health` re-used).

22. **Gate-counterfactual cross-window validation.**
    *2026-05-20 audit follow-up.* The daily review's
    `gate_counterfactual_health` block runs ONE window
    (trailing-30d) and labels recommendations HIGH /
    MEDIUM / LOW confidence based on within-window sample
    size. The 2026-05-20 P2 audit found the
    "HIGH-confidence" `gate_min_current_total 4 -> 5`
    recommendation reverses direction on lifetime data
    (30d blocked-cohort ROI -20.2% vs lifetime +4.5%, n=29)
    and would block 14 post-calibrator-enforce bets that
    are 12-2 with +35.7% ROI -- shipping it would have
    actively hurt P&L. Root cause: the report doesn't
    cross-check against lifetime or against the
    post-calibrator regime. Two fixes for the report:
    (a) when a within-window recommendation fires, also
    compute the same cohort over lifetime and flag a
    `window_reversal` warning if the direction inverts;
    (b) when the runtime calibrator's `_prob_calibration_mode`
    is `enforce`, the report should run the counterfactual
    on POST-calibrator FV (the actual current decision
    surface), not on raw FV. Until shipped, treat all
    HIGH-confidence gate-counterfactual recommendations
    as "candidate for audit," not "ready to ship." Files:
    `scripts/analysis/build_gate_counterfactual_report.py`,
    `scripts/analysis/human_review/core_health.py`
    (`_gate_counterfactual_health`).

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

A5. **Live UNDER candidate emission.** *Shipped 2026-05-19.* See
    the Recently Completed "Phase A5: live UNDER candidate
    emission (shadow mode)" entry for full details. Shipped as a
    `--under-emission-mode {off, shadow}` CLI flag on
    live_engine_cli.py. When `shadow`, the engine emits a sibling
    UNDER candidate row alongside every OVER candidate that
    reaches the FV phase, with its own calibrated FV
    (= UNDER_calibrator(1 - over_fv_raw)), its own UNDER-side
    market data (ask, bid, pair_available), and its own gate
    evaluation (`decision=shadow_under` when gates pass;
    `gate_min_edge` / `gate_no_under_liquidity` skip reasons
    otherwise). NO UNDER bets are placed in either mode (paper
    or live); pure observability so the paper-mode runway
    accumulates UNDER signal-quality data that the daily-review
    `by_side` block, training table, loss-attribution, and
    shadow-override reports all pick up automatically. The
    eventual UNDER paper-bet flip is a separate ship gated by
    B4 60-session validation.
    - Files: `scripts/trading/signal_config.py` (CLI flag +
      constants), `scripts/trading/signal_engine.py` (UNDER
      calibrator load), `scripts/trading/signal_pipeline.py`
      (`_maybe_emit_under_candidate` helper + wire site),
      `scripts/trading/live_engine_cli.py` + `live_engine.py`
      (CLI flag + trade_args bridge).

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
