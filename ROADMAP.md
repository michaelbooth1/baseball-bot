# Roadmap To The State-Value Objective

**Companion top-level docs:**
- **[README.md](README.md)** — operator overview, gate stack, evidence
  snapshot, CLI usage. Read this for the *what* and *why* before touching
  code.
- **[MASTER_CONTEXT.md](MASTER_CONTEXT.md)** — repo map; one entry per
  top-level folder pointing at its AGENT_CONTEXT.md. Read this when you
  need to know where something lives.
- **[ROADMAP_ARCHIVE_2026_H1.md](ROADMAP_ARCHIVE_2026_H1.md)** — shipped work
  from 2026-05-17 and earlier (~80 entries).

The roadmap aligns code, data, and gate enforcement with the state-value
transition objective. It is split into six sections:

- **Recently Completed** -- shipped infrastructure that earlier roadmap
  versions still listed as open. Older entries (2026-05-17 and earlier)
  have been moved to **[ROADMAP_ARCHIVE_2026_H1.md](ROADMAP_ARCHIVE_2026_H1.md)**;
  this section now keeps only the trailing ~7 days.
- **Active Priorities** -- ordered by what most needs to happen next, given
  the post-TR20/TR21 reality (Stage-3 v2 + Stage-2 density_alt/hr_factor).
- **Hygiene** -- accumulating debt; not blocking, but worth closing on a
  regular cadence so the loop stays clean.
- **Research findings** -- long-form analysis outputs and the deeper-dive
  follow-ups they suggested. Distinct from Hygiene because each item
  is a research question (conditioned on more data or further analysis),
  not a known shipping action.
- **Bidirectional trading -> market-making (long-horizon)** -- the
  multi-phase pivot from Over-only directional bets to a two-sided "smart
  market maker" trying to turn a profit on every game through high-volume
  quoting. Strategic, not a one-week task; phases A-E.
- **Operational guidance** -- standing rules for day-to-day session work.

## Verdict status dashboard

One-line status per Active and Hygiene item, with the artifact path the
operator should read next. Use this to triage; full text is below.

### Active priorities

| # | Item | Status | Blocked by | Read |
|---|---|---|---|---|
| 1 | Post-TR20+TR21 walk-forward + re-certify gates | **PRELIMINARY** (137 filled / 34 dates; need 150 / 30) | Sample size; expected READY ~2026-06-10 | `data/analysis_output/walk_forward_certification/walk_forward_certification.md` |
| 2 | Orphan-fill reconciler — primary vs safety-net | Watching | Need ≥30 sessions of reconciler-fired data | Daily review "Orphan-Fill Reconciler" section |
| 3 | Shadow → enforce gate promotions (`gate_ask_max=0.85`, `current_state_edge_min`) | Shadow | Active #1 verdict at READY | Walk-forward cert verdicts |
| 4 | Realized-EV trade rule + state-value guardrails | Structured in `live_ev_policy_runtime.py` (shadow) | Active #1 fill-rate curves | `data/analysis_output/ev_policy/` |
| 5 | Calibrated-edge stake scaling (Active #6 part 2) | Shadow; awaiting promotion verdict | `analyze_stake_scaling_promotion.py` verdict says `need_more_data` | Weekly rollup → "Active #6 stake-scaling promotion" panel |
| 6 | Learned execution policy → replace fixed spread heuristic | Prototype shipped; refreshes daily | ~200+ bets (expected ~2026-06-15); Active #2 conclusion | `data/analysis_output/execution_policy_prototype/learned_execution_policy_report.md` |
| 7 | No-score drift promotion path | Paper-only (no-score walk-forward refreshes daily) | 60+ post-TR20 days of durable empirical-support ROI | `data/analysis_output/no_score_drift_walk_forward/` |
| 8 | Stage-1 Alt-A promotion (full enforce; TR25 scoped enforce already shipped) | Scoped enforce live; full Alt-A promotion awaits paper-mode validation | Stable Alt-A staging cache + runtime shadow bias reduction in real outcomes | `data/analysis_output/stage1_shadow_override/` |
| 9–17 | (shipped 2026-05-17 → 2026-05-21) | ✅ Closed | — | [ROADMAP_ARCHIVE_2026_H1.md](ROADMAP_ARCHIVE_2026_H1.md) |

### Hygiene

| # | Item | Cost | Expected impact | Blocked by |
|---|---|---|---|---|
| 1 | Line-5.5 high-FV slice guard | One-line gate | **Shipped as `K_line5p5_block` paper preset 2026-05-26.** Gate exists at `gate_line_high_fv_block` (default OFF in production); K vs A_current is the A/B-test for promotion. Cheapest documented win — line=5.5 at raw_fv>=0.90 is 51% realized vs 96% claimed (n=92). | ~30 days of K vs A evidence |
| 2 | Mid-band [0.80, 0.90) calibrator under-confidence refit | Per-line + per-family Platt fit, or swap to isotonic | Closes the band that the TR23 band gate currently leaves uncorrected | Calibration training data freshness |
| 3 | Replace Poisson tail with negative-binomial fit | Larger model change | Structural fix for the 4-7pp Poisson > empirical bias at FV>=0.85 across all lines | Active #8 (Stage-1 Alt-A) demonstrates cohort-aware promotion machinery first |
| 4 | Chain-rebuild stale-input artifacts | Investigate-and-reorder OR auto-chain-rebuild | Removes the 5 cross-artifact consistency alerts firing every refresh | Root-cause investigation of `signal_training_table.jsonl` post-build mutation |
| 5 | Gate-counterfactual cross-window validation | **✅ Shipped 2026-05-26** | New 4th window `lifetime_post_calibrator_enforce`, `window_reversal` flag on recommendations, confidence auto-downgraded to `review_required` when 30d direction inverts on lifetime, daily-review block surfaces reversal alerts and suppresses reversed recs from the actionable Notes feed. First production run: 4 of 9 previously-HIGH-confidence top recommendations flagged as reversed — including the exact 2026-05-20 P2 audit example (`gate_min_current_total 4→5`: 30d=+$44.31 vs lifetime=−$151.97 on n=40). Closed. |

### Research findings

| # | Finding | Status | Most-actionable follow-up | Read |
|---|---|---|---|---|
| RF1 | Edge Atlas — market structurally overprices Over by **+2-5pp across every cohort** (lines 9.5/10.5/11.5 + inn_8-9 worst), validating bidirectional pivot premise | ✅ Shipped 2026-05-27 | RF1.a (recent-N comparison) tests whether 10y baseline is stale before any live use of the finding | `data/analysis_output/edge_atlas/edge_atlas.md` |
| RF1.a | Recent-N comparison: bias **survives directionally** across 3y/4y/5y/6y/10y windows (0 sign flips / 16 buckets), magnitude varies. Verdict: `BIAS_PARTIALLY_SURVIVES`. Bidirectional pivot premise confirmed regime-stable. | ✅ Shipped 2026-05-27 | RF1.b realized-outcomes deep dive (awaits more N), RF1.c per-park stratification | `data/analysis_output/edge_atlas/recent_n_comparison.md` |

Last dashboard refresh: **2026-05-27**. Refresh after each ship.

---

Last roadmap review: **2026-05-27 (latest)** (RF1.a Recent-N Edge
Atlas comparison shipped. New
`scripts/analysis/compare_edge_atlas_windows.py` runs the existing
`build_atlas_payload` once per cache window (3y/4y/5y/6y/10y),
rolls per-cohort biases into a comparison matrix, and emits a 4-way
verdict (`BIAS_SURVIVES_RECENT` / `BIAS_PARTIALLY_SURVIVES` /
`BIAS_STALE_REGIME_DRIFT` / `INSUFFICIENT_DATA`). **First
production run**: `BIAS_PARTIALLY_SURVIVES` — 0 sign flips across
16 cohort buckets × 5 windows; max |Δ| 1.78pp on extras innings;
aggregate biases all positive in the 2-3pp range across every
window. The RF1 finding survives directionally — the Over IS
overpriced across every measured historical regime — but the
magnitude varies (4y window actually shows the largest bias, not
the longest window). Bidirectional pivot premise confirmed
regime-stable; treat RF1 as directionally reliable for post-B4
go/no-go decisions; expect cohort-level magnitude drift but not
flipped signs. 20 new pytest cases, full suite **1560 tests + 41
subtests** green.). Earlier same day: **2026-05-27 (later)**
(B4 milestone dashboard shipped: closes the Phase C-paper loop. New
`_under_paper_b4_milestone_health` block walks paper_root +
live_root sessions across the trailing 60d, accumulates ACTUAL
`side="under"` paper bets, and reports verdict status across the
5 B4 conditions (sessions ≥60, n_settled ≥150, ROI >0%,
|calibration delta| ≤5pp, UNDER drift alerts <3/7 days). Verdict
ladder: `NOT_EMITTING → INSUFFICIENT_SESSIONS →
INSUFFICIENT_OUTCOMES → SUB_ZERO_ROI → CALIBRATION_OFF →
DRIFT_ALERT_PERSISTENT → READY`. Each failure ≥30 settled emits
to Notes with `Under-B4:` prefix; READY alert always fires; drift
scanner explicitly excludes `under-b4:` prefix to prevent
self-loop. First production run on 2026-05-17 session correctly
reports `NOT_EMITTING` baseline. 22 new pytest cases, full suite
**1540 tests + 41 subtests** green. The operator can now read the
B4 verdict + remaining gap in one block every refresh; once
`--under-mode paper` accumulates 60 sessions the dashboard will
walk through the verdict ladder until `READY` fires.). Earlier
same day: **2026-05-27 (later)** (Phase C-paper shipped:
the live-engine UNDER paper-bet path that B4 was waiting on. New
`--under-mode {off, shadow, paper}` CLI flag with backward-compat
alias for the old `--under-emission-mode`. `paper` runs the 5
symmetric UNDER gates (`gate_under_min_inning`,
`gate_under_min_entry_ask`, `gate_under_max_base_fv`,
`gate_under_fv_ask_gap`, `gate_under_extreme_edge`) +
`gate_min_edge`, then records a `BetRecord(side="under")` to
`engine._bets` that the standard settlement loop picks up.
Settlement now reads `bet.side` and flips to `final_total < line`
for UNDER. `LiveTradingEngine._is_bet_executable` overridden so
UNDER paper bets settle even when hosted inside the live engine.
Live CLOB `place_bet` carries an explicit "OVER-only by
construction" docstring + test that pins the contract — UNDER
paper bets bypass it entirely via `_place_under_paper_bet`.
**Defer** asymmetric OVER gates (pace, runs_needed, close_game,
inn5/6 dead zone, blowout, S2 suppress, pitcher boost) — they
flip direction for UNDER (e.g. blowout suppresses scoring = bad
for OVER, good for UNDER) and need UNDER-specific design after
paper data accumulates. **29 new pytest cases**, full suite green
at **1518 tests + 41 subtests**. Unblocks B4 — the operator can
now run `python scripts/trading/live_engine.py --under-mode paper`
and accumulate the 60 sessions of UNDER paper evidence the B4
verdict needs before live UNDER trading + quote-engine `act` flip
can ship.). Earlier same day: **2026-05-27** (Edge Atlas research day: new `build_edge_atlas.py` joins 10y MLB cache to ~1mo Polymarket ticks → 424k clean obs / 7,609 qualifying (cell × line) pairs. **Headline**: market structurally overprices Over by **+2-5pp across every cohort**, worst on lines 9.5/10.5/11.5 + late innings. Confirms the bidirectional pivot's structural premise — UNDER betting has lifetime +EV against the cache; OVER-only trades pay a 2-5pp premium vs 10y history. Documented as new **Research findings** section (RF1) with 6 follow-ups: RF1.a recent-N comparison [highest priority — tests whether 10y baseline is stale], RF1.b realized-outcomes deep dive [awaits more N], RF1.c per-park stratification, RF1.d frontend heatmap, RF1.e refresh-cadence wiring, RF1.f shadow-override pairing). Earlier: **2026-05-26** (Hygiene #5 shipped: gate-counterfactual cross-window validation; 4 of 9 production HIGH-confidence recommendations correctly flagged as window-reversals on first run, including the exact 2026-05-20 P2 audit example. Earlier same day: Hygiene #1 shipped as paper preset K_line5p5_block + F-J aggregator normalization added so high-volume configs can be read against A_current on equal per-bet footing). Prior review: **2026-05-25** (audit-driven refresh: split ROADMAP archive at 2026-05-17 into ROADMAP_ARCHIVE_2026_H1.md, renumbered Active priorities 1-8 sequentially with shipped items 9-17 consolidated to one line, promoted hygiene items 18-22 into a real ## Hygiene section as #1-#5, added the Verdict status dashboard above, reconciled Active #5 status with the shipped stake-scaling code). Prior review: **2026-05-19** (Band-gated calibrator
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

- **Fleet prune + paired-delta audit quick wins** *(2026-06-10, latest)* —
  first decision pass driven by the 2026-06-10 paired-delta fleet audit
  (the per-engine marginal tables hide the signal; the information is
  in the delta bets each config takes/skips vs A_current). Four ships:

  1. **E_tight_edge + G_loose_edge CONCLUDED + retired.** The fleet
     answered its edge-floor question in both directions: E (+5pp
     floor) skipped 41 bets that won 73.2% (net −$38 for tightening);
     G (−5pp floor) added 40 marginal-edge bets that won only 65.0%
     (net −$15 for loosening). **The 0.15 edge floor is locally
     optimal.** Matches the walk-forward cert's edge-band table
     (0.10-0.15 = −28.1% ROI; 0.15-0.22 = +14.2%). Both presets
     removed from PRESETS + default launch list; conclusions recorded
     as retirement comments in `launch_parallel_engines.py`.
  2. **N_extreme_edge_022 retired as a NULL experiment.** Its premise
     ("production runs --extreme-edge-max 1.0") was wrong — A_current
     ran the 0.22 signal_config default, identical to N. 10 days /
     47 settled bets / ZERO delta decisions vs A_current. The
     0.22-vs-0.30 question is owned by the 2026-06-03 live promotion
     + armed fast Wilson-UB demote; J_no_phantom_filter keeps
     providing the edge>0.30 counterfactual cohort.
  3. **A_current re-synced to live.** Live runs gate_extreme_edge=0.30
     via cache/live_engine_overrides.json (2026-06-03 promotion), but
     paper engines don't read the overrides file — the baseline arm
     had silently drifted to 0.22, confounding every X-vs-A
     comparison. A_current now passes `--extreme-edge-max 0.3`
     explicitly. (Phantom-band 0.70 needs no flag: signal_config
     default already matches the live override.)
  4. **B4 scanner blind spot fixed.** The B4 milestone walked only
     `data/paper_trading/sessions` + live sessions, so UNDER paper
     bets written by the M_under_paper fleet preset (which writes to
     `data/paper_M_under_paper/sessions`) never advanced the
     60-session clock — dashboard read 0/60 while evidence
     accumulated invisibly since 2026-05-30. New
     `B4_EXTRA_PAPER_SESSION_ROOTS` constant + threaded
     `extra_paper_sessions_dirs` through the collector (bet_id dedup
     across all roots; sources labeled `fleet:<root>`). First real
     run: status NOT_EMITTING → INSUFFICIENT_SESSIONS (4/60 sessions,
     8 settled). Caveat: the 2026-06-02 day carries 5 multi-fire dup
     bets from the pre-fix UNDER dedup leak; same_game_multi_fire
     health already flags these, and B4's verdict conditions are
     n-gated far above this contamination level.

  Fleet now runs 11 presets (was 14). Tests: preset-shape tests
  updated, +3 new B4 fleet-root tests, B4 ladder tests isolated from
  the production extra-roots default.

- **RF1.a Recent-N Edge Atlas comparison** *(2026-05-27)* —
  tests whether the 2026-05-27 Edge Atlas RF1 finding (+2-5pp Over
  premium across every cohort, measured on the 10y MLB Stage-1
  cache) survives across multiple historical windows or whether
  it's a 10y baseline artifact (juiced ball era, fence moves,
  recent scoring-environment shifts). The strongest pre-pivot
  evidence we have for the bidirectional / market-maker pivot
  depends on this answer — if RF1 is a regime artifact, post-B4
  go/no-go calls need a caveat; if it survives, RF1 is a
  high-confidence input.

  **Script**: new `scripts/analysis/compare_edge_atlas_windows.py`.
  Runs the existing `build_atlas_payload` (from
  `build_edge_atlas.py`) once per Stage-1 cache window (no cache
  builds needed — all 5 windows exist pre-comparison), rolls the
  per-window `by_inning_band` / `by_line` / `by_score_diff_band`
  cohort summaries into a comparison matrix vs the
  `10y_2016_2025` baseline, and emits a verdict.

  **Windows compared**:
  - `3y_2023_2025` → `cache/mlb_ou_cache_3y_2023_2025_candidate.json`
  - `4y_2022_2025` → `cache/mlb_ou_cache_4y_2022_2025_candidate.json`
  - `5y_2021_2025` → `cache/mlb_ou_cache_5y_baseline_2021_2025.json`
  - `6y_2020_2025` → `cache/mlb_ou_cache_6y_2020_2025_candidate.json`
  - `10y_2016_2025` → `cache/mlb_ou_cache_10y_candidate.json` (baseline)

  **Verdict ladder**:
  - `BIAS_SURVIVES_RECENT`: every cohort within ±1.5pp of baseline
    AND no sign flips → high confidence
  - `BIAS_PARTIALLY_SURVIVES`: signs consistent, some cohorts in
    [1.5pp, 3pp] from baseline → directionally robust, magnitude varies
  - `BIAS_STALE_REGIME_DRIFT`: any sign flip OR any |Δ| ≥ 3pp →
    10y baseline misleading, add caveat to bidirectional evidence
  - `INSUFFICIENT_DATA`: too few overlapping cohort buckets

  Sign flips use a ±0.5pp tolerance around zero so tiny near-zero
  biases don't artificially count as flips.

  **First production run** (2026-05-27 + ~1mo of Polymarket data):
  - **Verdict**: `BIAS_PARTIALLY_SURVIVES`.
  - **0 sign flips** across 16 cohort buckets × 5 windows.
  - **Max |Δ|** = 1.78pp on extras innings (inn_10+, 5y shows
    +4.05pp vs 10y +5.83pp — still strongly positive but
    magnitude differs).
  - **Aggregate stake-weighted biases**:
    | Window | Bias |
    |---|---|
    | 3y_2023_2025 | +2.41pp |
    | 4y_2022_2025 | +3.18pp |
    | 5y_2021_2025 | +2.91pp |
    | 6y_2020_2025 | +2.78pp |
    | 10y_2016_2025 | +2.46pp |
  - **By line**: every line bucket positive in every window,
    max |Δ| 1.17pp (7.5 line).
  - **By score diff**: every band positive in every window,
    max |Δ| 1.25pp (trailing_1-3 band).
  - **By inning band**: every inning bucket positive in every
    window; the inn_10+ extras innings bucket drives the
    headline max |Δ|.

  **Operator takeaway**: the directional finding (Over IS
  overpriced relative to historical empirical) is robust across
  every measured regime; the magnitude varies but never enough to
  flip the conclusion. The 4y window actually shows the LARGEST
  bias (+3.18pp), not the longest window — counterintuitive
  evidence against "10y baseline is inflating the finding."
  Treat RF1 as directionally reliable for post-B4 bidirectional-
  pivot go/no-go decisions; expect cohort-level magnitude drift
  but no flipped signs.

  **What this unlocks**: post-B4, the operator can lean on RF1
  as structural evidence that bidirectional trading captures a
  real premium. RF1.b (realized-outcomes deep dive) becomes the
  next follow-up once enough actual UNDER paper bets accumulate
  to test "does our paper UNDER WR confirm the cohort biases
  this report measures?".

  **Output**:
  - `data/analysis_output/edge_atlas/recent_n_comparison.json`
  - `data/analysis_output/edge_atlas/recent_n_comparison.md`

  **Tests**: 20 new in `tests/test_compare_edge_atlas_windows.py`:
  cohort matrix arithmetic (basic two-window delta, missing-bucket
  None handling, sign-flip detection, near-zero tolerance, summary
  aggregation), verdict classifier (all 4 verdict states +
  cross-cohort-dimension aggregation), stake-weighted aggregate
  bias from atlas payload (stake-weight math, n_games/n_ticks
  floor exclusions, empty handling), build_comparison_payload
  integration (missing-cache reporting, baseline fallback,
  research_id wiring), markdown render smoke. **1560 tests + 41
  subtests pass** (+20).

  **Files**:
  - `scripts/analysis/compare_edge_atlas_windows.py` (NEW, ~530 LOC)
  - `tests/test_compare_edge_atlas_windows.py` (NEW, 20 tests)
  - `data/analysis_output/edge_atlas/recent_n_comparison.{json,md}`
    (NEW, first production run output)

- **B4 milestone dashboard** *(2026-05-27, later)* — closes the
  Phase C-paper loop. Before this ship, the operator could turn on
  `--under-mode paper` but had no visibility into how close the
  60-session B4 validation timer was, which condition was the
  current limiter, or when verdict would clear. The existing
  trailing-7d `under_outcomes_counterfactual_health` block tracks
  SHADOW counterfactual P&L — useful for sanity-checking signal
  quality but it does NOT advance B4 (B4 specifically requires
  ACTUAL paper bets, not counterfactual rollups).

  **New block**: `_under_paper_b4_milestone_health` in
  `scripts/analysis/human_review/under_health.py`. Walks BOTH
  `data/paper_trading/sessions/` AND `data/live_trading/sessions/`
  (an operator running the live engine with `--under-mode paper`
  accumulates evidence on the live root) across the trailing 60d
  and dedupes by `bet_id` so a bet hosted in both roots counts
  once per date.

  **5 verdict conditions** (from ROADMAP B4 spec):
  1. `sessions_with_under_bets >= 60`
  2. `n_settled >= 150`
  3. `taker_roi > 0%`
  4. `|calibration_delta_pp| <= 5pp` (realized WR vs predicted WR
     using `mean(bet.fair_value)` as predicted)
  5. UNDER drift alerts on `<3 of last 7d` of human-review JSONs
     (B1 dimension family; scanner matches `under:` / `under-`
     prefixes in Notes but EXPLICITLY EXCLUDES the new `under-b4:`
     prefix so today's verdict alert can't pollute tomorrow's
     drift count — self-loop guard)

  **Verdict ladder** (highest-priority gap surfaces first):
  ```
  NOT_EMITTING -> INSUFFICIENT_SESSIONS -> INSUFFICIENT_OUTCOMES
    -> SUB_ZERO_ROI -> CALIBRATION_OFF -> DRIFT_ALERT_PERSISTENT
    -> READY
  ```

  **Per-condition status** also surfaced in payload regardless of
  verdict so the operator can see all 5 in one block:
  ```json
  "conditions": {
    "sessions": {"value": 28, "target": 60, "remaining": 32, "pass": false},
    "n_settled": {"value": 83, "target": 150, "remaining": 67, "pass": false},
    "roi": {"value": 0.034, "min_roi": 0.0, "pass": true},
    "calibration_delta_pp": {"value": -2.3, "tolerance_pp": 5.0, "pass": true},
    "under_drift_alerts": {"days_with_alert": 0, "lookback_days": 7, "persistence_threshold": 3, "pass": true}
  }
  ```

  Plus `aggregate` (full trailing metrics + first/last session
  date) and `by_date` drill-down with per-day n_under_placed,
  n_settled, wins, profit, roi, sources (which roots had data).

  **Alerts** mirrored to Notes via `Under-B4:` prefix:
  - **READY**: always fires when status=READY (good news; surface
    it regardless of n).
  - **SUB_ZERO_ROI / CALIBRATION_OFF / DRIFT_ALERT_PERSISTENT**:
    fire only when `n_settled >= 30` (`min_n_for_failure_alert`
    default) — avoids spamming false alarms on tiny samples while
    still showing per-condition status in the JSON for audit.
  - Quiet states (`NOT_EMITTING`, `INSUFFICIENT_SESSIONS`,
    `INSUFFICIENT_OUTCOMES`) emit NO Notes alert — milestone
    progress is already implicit in the existing trailing-7d
    `under_outcomes_counterfactual_health` block.

  **First production run** against today's daily review
  (2026-05-17 session): `status=NOT_EMITTING`,
  `aggregate.n_sessions_with_under_bets=0`, 0 alerts. Correct
  baseline -- the operator has not yet run `--under-mode paper`
  for a complete session. Once paper UNDER bets land, the block
  will start ticking through the verdict ladder automatically;
  the operator will see the same JSON block every day with
  evolving values until verdict reads `READY`.

  **Files**:
  - `scripts/analysis/human_review/constants.py` (+~25 LOC: new
    `DEFAULT_PAPER_SESSIONS_DIR` + 7 B4 threshold constants)
  - `scripts/analysis/human_review/under_health.py` (+~290 LOC:
    new `_load_session_bets`, `_collect_paper_under_bets_for_date`,
    `_aggregate_paper_under_bets`,
    `_count_persistent_under_drift_alerts`,
    `_under_paper_b4_milestone_health` helpers)
  - `scripts/analysis/human_review/__init__.py` (+~10 LOC: export
    4 new helpers)
  - `scripts/analysis/build_daily_human_review_report.py`
    (+~20 LOC: import + call + Notes wiring + return-dict entry)
  - `tests/test_under_paper_b4_milestone.py` (NEW, 22 tests
    spanning verdict ladder transitions, cross-root union dedup,
    drift-alert self-loop guard, aggregate arithmetic, alert
    emission gating, build_report integration)

  **Tests**: full pytest suite **1540 passed + 41 subtests, 0
  regressions** (was 1518; +22 new).

- **Phase C-paper: UNDER paper-bet path** *(2026-05-27)* — closes
  the prerequisite that was blocking B4. Before today the only
  thing standing between the operator and the 60-session UNDER
  validation runway was the code: A5 (2026-05-19) shipped UNDER
  candidate emission in shadow mode but explicitly never placed,
  and Phase C as documented bundled three pieces (live UNDER
  trading + UNDER post-FV gates + quote-engine `act`) that all
  required B4 evidence we didn't yet have. This ship splits the
  bundle so the paper-mode prerequisite ships now and the
  live-flip pieces stay gated by the B4 verdict as designed.

  **CLI** (`signal_config.py` + `live_engine_cli.py`): renamed
  `--under-emission-mode {off, shadow}` → `--under-mode {off,
  shadow, paper}`. Legacy alias `--under-emission-mode` kept for
  one transition cycle (silent backward-compat; new flag wins if
  both passed). Post-parse normalizer in `parse_trade_args`
  resolves precedence + mirrors back to `trade_args.under_emission_mode`
  so any downstream reader of the old attr keeps working. 8 new
  `--under-*` gate-threshold flags expose the symmetric stack
  (`--under-min-inning`, `--under-min-inning-high-line`,
  `--under-min-entry-ask`, `--under-min-entry-ask-high-line`,
  `--under-max-base-fv`, `--under-fv-ask-gap-max`,
  `--under-fv-ask-gap-min-inning`, `--under-extreme-edge-max`);
  each defaults to its OVER counterpart so a fresh operator
  starts with parity.

  **UNDER gate stack** (`signal_pipeline.py
  ::_maybe_emit_under_candidate`): 5 symmetric gates evaluated
  in order before the existing `gate_min_edge`:
  1. `gate_under_min_inning` — variance reduction (same logic
     applies to both sides).
  2. `gate_under_min_entry_ask` — thin-book guard on UNDER ask.
  3. `gate_under_max_base_fv` — UNDER FV saturation / phantom
     no-score.
  4. `gate_under_extreme_edge` — symmetric application of TR19's
     OVER-side empirical finding (very large edge = market more
     informed than us).
  5. `gate_under_fv_ask_gap` — late-inning large gap (only when
     `inning >= --under-fv-ask-gap-min-inning`).

  **Asymmetric gates deliberately deferred** (pace, runs_needed,
  close_game, inn5/6 dead zone, blowout, S2 suppress, pitcher
  boost): they work in the OPPOSITE direction for UNDER (e.g.
  blowout suppresses scoring — bad for OVER, GOOD for UNDER) so
  mirroring them naively would over-block. They need UNDER-
  specific design after paper data accumulates; flagged as
  Hygiene candidates once shadow data has enough cohort N.

  **Paper placement** (new `_place_under_paper_bet` helper):
  builds a `BetRecord(side="under")` and appends to `engine._bets`
  so the standard settlement loop picks it up. Capture sidecars
  (book / tape / velocity) intentionally skipped — those are OVER-
  bet-focused; UNDER paper only needs the BetRecord. UNDER bet ids
  use a separate counter and `_under_NNNN` suffix so OVER + UNDER
  bet ids never collide.

  **Settlement** (`signal_engine.py::_settle_finished_games`):
  reads `bet.side` and computes `counterfactual_won = (final_total
  < line)` for UNDER vs `> line` for OVER. MLB OU lines end in .5
  so pushes are structurally impossible. SETTLED / MISSED log
  lines now show the actual `bet.side.upper()` instead of the
  previously-hardcoded "OVER".

  **Live-engine safety** (`live_engine.py::_is_bet_executable` +
  `live_engine_placement.py::place_bet`): the live `_is_bet_executable`
  override (which gates OVER bets on `order_status == "filled"`)
  now short-circuits to True for `side == "under"`, because UNDER
  paper bets are filled-at-limit by construction and never have
  an order_status. The CLOB `place_bet` carries an explicit
  "Phase C-paper invariant: OVER-only by construction (uses
  market.over_token_id); UNDER bets are paper-only and bypass
  this path entirely via _place_under_paper_bet" docstring + a
  test that pins the contract so accidental removal trips CI.

  **What this unlocks**:
  - Operator can now run
    `python scripts/trading/live_engine.py --under-mode paper`
    (or the same flag on `paper_trader.py`) and start the
    60-session B4 clock. UNDER paper bets land in the same
    session JSON as OVER bets with `side="under"`, settle
    correctly, and populate the `by_side.under` block of daily
    review automatically.
  - The existing UNDER outcomes counterfactual block (which has
    been silently accumulating shadow_under rows since 2026-05-19)
    continues to work; paper rows arrive as `decision="paper_under"`
    so cohort analysis can split paper vs shadow.

  **What stays deferred** (gated by B4 verdict per design):
  - `--under-mode live` value: not in the CLI choices list yet;
    the parser rejects it. Operator must complete B4 before this
    value is added.
  - `--quote-engine-mode act` flip: still shadow-only.
  - UNDER-side hedging execution.
  - The full UNDER post-FV gate stack tuned from UNDER data
    (today's 5 symmetric gates use OVER's empirical thresholds;
    UNDER-specific tuning comes from B4 cohort analysis).

  **Tests**: 29 new in `tests/test_under_paper_placement.py`
  spanning `UnderGateStackTests`, `UnderPaperPlacementTests`,
  `UnderSettlementTests`, `LiveEngineUnderSafetyTests`,
  `UnderModeFlagTests`. Existing `tests/test_under_candidate_emission.py`
  updated to set permissive UNDER gate defaults in
  `_make_fake_engine` so 11 pre-existing shadow tests don't trip
  the new 5-gate stack; bridge test pattern-matches the new
  live_engine.py wiring. **1518 tests + 41 subtests pass** (+29
  from this ship; 0 regressions on the rest of the suite).

  **Files**:
  - `scripts/trading/signal_config.py` (+~115 LOC: new constants,
    `--under-mode` + 8 gate flags, post-parse alias merge,
    validation block)
  - `scripts/trading/live_engine_cli.py` (+~16 LOC: new
    `--under-mode` flag + legacy alias)
  - `scripts/trading/live_engine.py` (+~24 LOC: bridge for new
    flag + 8 gate thresholds, `_is_bet_executable` UNDER override)
  - `scripts/trading/signal_engine.py` (+~30 LOC: read new attr
    with legacy fallback, settlement reads bet.side, SETTLED/
    MISSED log line uses actual side)
  - `scripts/trading/signal_pipeline.py` (+~270 LOC: new
    `_place_under_paper_bet` helper, expanded
    `_maybe_emit_under_candidate` with 5-gate stack +
    `paper_under` decision tag + paper placement call)
  - `scripts/trading/live_engine_placement.py` (+~9 LOC:
    "Phase C-paper invariant" docstring callout)
  - `tests/test_under_paper_placement.py` (NEW, 29 tests)
  - `tests/test_under_candidate_emission.py` (~25 LOC: permissive
    UNDER gate defaults in `_make_fake_engine`; bridge test
    updated to match new wiring)

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
<!-- Older entries (2026-05-17 and earlier) have been archived. -->
<!-- See [ROADMAP_ARCHIVE_2026_H1.md](ROADMAP_ARCHIVE_2026_H1.md). -->

_For shipped work from 2026-05-17 and earlier, see_ **[ROADMAP_ARCHIVE_2026_H1.md](ROADMAP_ARCHIVE_2026_H1.md)** _(2,730 lines, ~80 entries)._


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

9-17. **Items 9-17 shipped 2026-05-17 -> 2026-05-21.** Per-cohort calibration drift detection, bet-level loss attribution, counterfactual gate-change logger, settlement-truth verification, fast Wilson-UB demotion, backup retention + PSI-history GC, promotion-lag tracker, model lineage tracking (v1-v4), and Scoped Alt-A enforce. All marked closed; full text in [ROADMAP_ARCHIVE_2026_H1.md](ROADMAP_ARCHIVE_2026_H1.md). The order is non-sequential because #17 (Scoped Alt-A) was inserted out of order during the 2026-05-21 ship and never renumbered.

> ✅ **Risk-note resolved 2026-05-26**: Hygiene #5 shipped. The
> gate-counterfactual report now stamps every top recommendation with
> a `lifetime_counterfactual_profit_delta_usd` + a
> `post_calibrator_counterfactual_profit_delta_usd` (4th window
> filters to bets after the 2026-05-19 calibrator-enforce flip), and
> flags `window_reversal=True` when the 30d direction inverts on
> lifetime with enough N/$ to be material. Confidence auto-downgrades
> to `review_required` and the daily-review notes feed suppresses
> reversed recommendations as actionable while keeping them visible
> in the structured payload. First production run on the existing
> training table found **4 of 9** previously-HIGH-confidence
> recommendations are window-reversals — including the exact
> 2026-05-20 P2 audit example (`gate_min_current_total 4 -> 5`:
> 30d=+$44.31 vs lifetime=−$151.97 on n=40). Operator can now trust
> any HIGH-confidence rec that survives the check.


## Hygiene

_Accumulating debt; not blocking, but worth closing on a regular cadence so the loop stays clean. Each item is shipped-ready (audit findings, file paths, expected impact) but waits on either bandwidth or a prerequisite._

1. **Line-5.5 high-FV slice guard.** *2026-05-19 audit
    follow-up; shadow shipped 2026-05-26 as `K_line5p5_block`
    paper preset.* Per the audit's per-line slice (1,376 settled
    OVER predictions), at raw FV >= 0.90 the realized hit rate
    for line=5.5 is **51% on n=92** vs claimed 96% -- the worst
    miss in the dataset, and the calibrator only partially
    rescues it. **Status as of 2026-05-26**: option (a) "hard
    per-line FV ceiling at 5.5" has shipped as a new gate
    `gate_line_high_fv_block` in `signal_pipeline_gates_post_fv.py`
    (default `--line-high-fv-block-mode off`, so production is
    unchanged). The K_line5p5_block paper preset
    (`scripts/trading/launch_parallel_engines.py` PRESETS) enforces
    it against the live market each day for A/B-test evidence vs
    A_current. **Bar to clear for live promotion**: ~30 days of
    K vs A evidence where K's per-bet ROI matches or beats A's
    on the suppressed-bet cohort (i.e., the bets K skips were
    indeed losers on aggregate). Then promote by flipping
    `DEFAULT_LINE_HIGH_FV_BLOCK_MODE` from `off` to `enforce` in
    `signal_config.py` (or set on the live CLI). Audit input data:
    `data/analysis_output/calibration/signal_win_calibration_predictions.jsonl`.
    Files (now shipped): `scripts/trading/signal_pipeline_gates_post_fv.py`,
    `signal_config.py`, `scripts/trading/launch_parallel_engines.py`.

2. **Mid-band [0.80,0.90) calibrator under-confidence
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

3. **Replace Poisson tail with negative-binomial fit.**
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

4. **Chain-rebuild stale-input artifacts.**
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

5. **Gate-counterfactual cross-window validation.** *✅ Shipped
    2026-05-26.* The 2026-05-20 P2 audit found the
    "HIGH-confidence" `gate_min_current_total 4 -> 5`
    recommendation reverses direction on lifetime data
    (30d blocked-cohort ROI -20.2% vs lifetime +4.5%, n=29)
    and would block 14 post-calibrator-enforce bets that
    are 12-2 with +35.7% ROI -- shipping it would have
    actively hurt P&L. Two fixes both shipped today:
    **(a)** `build_top_recommendations` now augments every
    recommendation with `lifetime_counterfactual_profit_delta_usd`
    + `lifetime_blocked_n_filled` (from the existing `all`
    window) and flags `window_reversal=True` when the
    30d direction inverts on lifetime with ≥10 lifetime-blocked
    N and ≥$20 lifetime |delta|. **(b)** New 4th window
    `lifetime_post_calibrator_enforce` (rows with
    `session_date >= 2026-05-19`, the calibrator-enforce-flip
    date) -- separately surfaces the post-calibrator regime
    so reversal can distinguish "regime change" from "real
    inversion." Confidence auto-downgrades to `review_required`
    on reversal, and `_gate_counterfactual_health` suppresses
    reversed recommendations from the actionable Notes feed
    while keeping them visible in the structured payload +
    raising a dedicated `Gate-counterfactual: ... flagged
    window_reversal` alert per reversed rec. **First
    production run on the existing training table**: 4 of 9
    previously-HIGH-confidence top recommendations flagged as
    reversed, including the exact P2 audit example
    (`gate_min_current_total 4 -> 5`: 30d +$44.31 vs
    lifetime -$151.97 on n=40). 6 new pytest cases cover
    window slicing for the new window, reversal flag firing,
    no-reversal when 30d/lifetime agree, no-reversal when the
    lifetime cohort is below the floor, and the daily-review
    block surfacing the alert + suppressing reversed recs.
    Files (shipped): 
    `scripts/analysis/build_gate_counterfactual_report.py`,
    `scripts/analysis/human_review/core_health.py`
    (`_gate_counterfactual_health`).

## Research findings

Long-form analysis outputs and the deeper-dive follow-ups they suggest.
Each finding is a structured research output (descriptive only -- not
a shipping decision in itself) plus the open questions it surfaced.
Each follow-up is conditioned on data accumulation, a code change, or
further analysis; promotion to live behavior requires walk-forward
validation on top of any finding here.

### RF1. Edge Atlas — market structurally overprices Over (2026-05-27)

**Output**: `data/analysis_output/edge_atlas/edge_atlas.{json,md,csv}`,
generated by `scripts/analysis/build_edge_atlas.py`.

**Method**: joined 10y MLB Stage-1 cache (`cache/mlb_ou_cache.json`,
4,298 cells × 6 lines) to ~1mo of Polymarket OVER-side candidate ticks
across all default data roots. Filters: OVER side only, no active
inference (stale-schedule guard), no boundary asks (≤0.01 / ≥0.99
are exchange settled markers), no wide-spread asks (ask−bid > 25c =
stale offer on a thin book). For every (cell × line) clearing the
40-game + 10-tick floor, computed `bias = market_ask_median −
p_empirical` and ranked by `|bias| × √min(n_ticks, 1000)`.

**First production run (2026-05-27)**: 424k clean observations across
380 distinct games; 7,609 qualifying (cell × line) pairs.

**🔥 Headline finding**: the market structurally OVERPRICES the Over
by +2-5pp across **every** cohort.

| Inning band | mean bias | stake-wtd bias | over-priced cells | under-priced cells |
|---|---:|---:|---:|---:|
| inn_1-3 | +1.9% | +1.8% | 1,359 | 721 |
| inn_4-5 | +3.6% | +3.5% | 1,374 | 417 |
| inn_6-7 | +3.8% | +3.9% | 1,407 | 284 |
| inn_8-9 | **+4.5%** | **+4.8%** | 999 | 152 |
| inn_10+ | +5.4% | +4.0% | 14 | 1 |

| Line | mean bias | stake-wtd bias |
|---|---:|---:|
| 7.5 | +1.1% | +0.4% |
| 8.5 | +3.2% | +2.9% |
| **9.5** | **+4.6%** | **+4.6%** |
| 10.5 | +4.0% | +3.6% |
| **11.5** | +4.3% | +4.1% |

Every score-diff band shows +3.1% to +3.7%. Every outs count and every
bases-mask category is positive. Bias is monotonically increasing with
inning and with line.

**Strategic implications**:
1. **Confirms the bidirectional pivot's central premise** (see next
   section). The market's systematic Over premium is exactly the
   structural inefficiency a two-sided quoter can capture.
2. **OVER-only trades pay a 2-5pp premium vs 10y history on every
   bet.** Quantifies the cost of the current strategy direction.
3. **TR23 band-gated calibrator does the right thing on this dimension**
   -- it pulls down the dangerous-tail FVs where the overpricing is
   highest, reducing how often we buy Over at the steepest premium.
4. **Highest-edge slices for UNDER exploration**: inn_8-9 + lines
   9.5/10.5/11.5 + bases empty (the cleanest mispricings in absolute
   terms; over 1,400 cells in inn_8-9 with overpricing dominance).

**Caveats** (must be addressed before any live behavior change):
- Descriptive only. The 10y baseline doesn't account for current-season
  scoring environment shifts (juiced ball, fence moves, humidor) or
  season-to-date trends. The +3% structural bias might be 0% or +6%
  today; we don't know without season-weighted analysis.
- Realized-outcomes overlay is small-n per cell (~3-6 game obs each),
  so per-cell realized rates are noisy. Direction across many cells
  is the load-bearing signal; individual cell outcomes are anecdote.
- Cell-level magnitudes are pre-calibrator. The TR23 band gate already
  corrects high-FV regions in OUR trading; the atlas measures the
  market's gap to RAW 10y truth, not the calibrated-FV gap.
- 380 games is ~6 weeks of monitoring. Some cells with high
  significance scores have very few unique games (n_games=2-5).
  Distinguishing structural bias from "the 3 games we saw happened
  to be weird" requires more N per cell.

**Deeper-dive follow-ups** (conditioned on more data or further work):

  **RF1.a — Recent-N empirical comparison.** Re-build the atlas using
  only the last 2-3 MLB seasons (2023-2025) instead of the full 10y
  baseline. If the bias is similar magnitude, it's structural and
  durable; if it shrinks materially, the 10y baseline is stale and
  the current opportunity is smaller than the atlas suggests. Cheapest
  test of whether the headline finding survives regime change.
  *Cost*: ~2-3 hours (add `--min-season` arg to `build_edge_atlas.py`
  + re-run; or build a side-by-side report comparing both baselines).
  *Decision unlocked*: how much weight to give the 10y atlas vs
  "the market is right today even if it wasn't 10 years ago."

  **RF1.b — Realized-outcomes deep dive (await more N).** Re-run the
  atlas after ~6-12 more weeks of monitoring so per-cell
  `realized_over_rate` reaches n≥20 per cell. The killer question is:
  in the cells where the atlas says bias=+5%, does the realized rate
  on the games we OBSERVED match the cache (+5% mispricing is real)
  or the market (+5% is market knowing something we don't)? Currently
  too noisy at n=3-6 per cell. *Cost*: ~1 hour of analysis once data
  accumulates (the overlay already exists in the builder). *Decision
  unlocked*: whether the bias is exploitable or just a backtest
  artifact.

  **RF1.c — Per-(park, weather) stratification.** The atlas aggregates
  across all parks/weather. The +3% structural bias might be uniform,
  OR might be concentrated at HR-friendly parks (Coors, Wrigley w/
  wind out) where the market has a known longshot-lover problem, with
  zero bias at pitcher-friendly parks. Splitting bias by park (and
  by `density_alt`/`hr_factor` Stage-2 buckets) would tell us
  whether the opportunity is universal or targeted. *Cost*: ~4-6 hours
  (extend the atlas with a 4th grouping dimension joined to
  `cache/park_hr_factors.json` and the active weather cache).
  *Decision unlocked*: which cells/parks to prioritize for UNDER
  emission + eventual quoting.

  **RF1.d — Frontend heatmap rendering.** Wire the atlas into a new
  frontend tab so the operator can browse the bias map interactively
  by (inning × line × score-diff). Each cell colored red/blue by
  bias magnitude; click to drill into per-game observations. Makes
  the analysis explorable instead of one-shot. *Cost*: ~3-4 hours.
  *Decision unlocked*: nothing directly, but raises the rate at which
  the operator notices patterns in subsequent atlas refreshes.

  **RF1.e — Atlas refresh cadence.** Currently `build_edge_atlas.py`
  is a one-shot CLI; nothing wires it into the daily refresh. Decide
  whether the atlas should refresh daily (the data drifts as more
  games are monitored) or only on-demand. If daily, wire it into
  `run_daily_refresh.py` after `signal_training_table`. *Cost*: ~1
  hour. *Decision unlocked*: turns the atlas from a one-shot research
  artifact into an ongoing surface the operator can check for drift.

  **RF1.f — Pair with stage1_shadow_override report.** The existing
  shadow-override report computes Alt-A bias reduction for our model;
  the atlas computes market bias vs cache. Joining them surfaces
  cells where BOTH our model and the market mispredict relative to
  10y history -- those are the cells where the structural truth is
  hardest to model, useful for prioritizing where to add features.
  *Cost*: ~2-3 hours. *Decision unlocked*: feature-engineering
  priorities.

## Bidirectional trading -> market-making (long-horizon)

The bot today is **Over-only**: it evaluates one side of every game,
quotes one direction, and books P&L on directional accuracy. The
long-horizon ambition is to become a **two-sided "smart market maker"**
on Polymarket MLB OU markets -- quote both bid and ask on every game,
turn a small profit per side through high volume + spread capture, and
use inventory management to stay roughly delta-neutral on outcomes
where our edge is in *spread* not *direction*.

**Structural validation (2026-05-27)**: the Edge Atlas (see Research
findings RF1 above) measured Polymarket OVER ask vs 10y MLB empirical
across 7,609 (cell × line) pairs and found a +2-5pp Over premium in
EVERY cohort. That's exactly the inefficiency a two-sided quoter
captures structurally — the directional Over-only strategy pays the
premium on every fill; a market-maker collects it on every UNDER fill.
The atlas is descriptive (and 10y baseline may not reflect today's
scoring environment), but the cohort universality makes it the
strongest pre-pivot evidence we have for the strategy.

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

B4. **UNDER paper-mode validation period.** *Runway open
    (2026-05-27)*. The `--under-mode paper` prerequisite shipped
    today (see Recently Completed "Phase C-paper" entry below) so
    the 60-session clock can now start. Before any UNDER bet
    touches real money:
    - **DONE**: Phase C-paper ships `--under-mode paper` + the 5
      symmetric UNDER gate stack (extreme_edge, fv_ask_gap,
      max_base_fv, min_inning, min_entry_ask).
    - **DONE**: B4 milestone dashboard shipped (see Recently
      Completed entry). `_under_paper_b4_milestone_health` block
      in the daily review tracks all 5 verdict conditions across
      the trailing 60d and surfaces verdict status
      (`NOT_EMITTING → INSUFFICIENT_SESSIONS → ... → READY`) so
      the operator can read remaining gap each session without
      manual derivation.
    - Operator runs the live engine with `--under-mode paper` for
      **>= 60 daily sessions**. Threshold matches the OVER
      walk-forward `READY` verdict's date floor (30 dates) doubled,
      because UNDER is in less-validated territory than OVER was
      when OVER promoted.
    - Across those 60 sessions, the daily review's `by_side.under`
      block must show:
      * `n_under_outcomes >= 150` (matches A4 walk-forward READY)
      * UNDER win rate within 5pp of the UNDER calibrator's
        predicted win rate (calibration is honoring outcomes)
      * UNDER taker ROI > 0% (positive expected value)
      * No persistent UNDER-side drift alerts from B1 (calibrator
        stable; cohort_roi will fire if outcomes diverge)
    - Only after the verdict clears: the operator can ship the
      `--under-mode live` value (currently rejected by the CLI
      parser) AND the `--quote-engine-mode act` flip. Both remain
      structurally unbuilt today by design — B4 protects against
      shipping them on unvalidated UNDER signal quality.

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
