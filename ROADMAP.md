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
| 1 | Post-TR20+TR21 walk-forward + re-certify gates | ✅ **READY 2026-06-10** (215 filled / 47 dates, ROI +5.1%). First verdict acted on: `gate_extreme_edge` 0.22→0.30 promoted 2026-06-03. Open verdicts: `gate_high_line_min_inning` 5→6 (RETUNE, low conf), `shadow_gate_current_state_edge_min` EXPLORE 0.08 | — | `data/analysis_output/walk_forward_certification/walk_forward_certification.md` |
| 2 | Orphan-fill reconciler — primary vs safety-net | Pass-2 (losing-side trade-history recovery) shipped 2026-06-07 after the CWS@PHI 11.5 race-condition incident; watching | Paused during paper-only week (no live fills); resume evidence clock on live re-entry | Daily review "Orphan-Fill Reconciler" section |
| 3 | Shadow → enforce gate promotions (`gate_ask_max=0.85`, `current_state_edge_min`) | **Unblocked** (Active #1 READY). Cert's cse sweep recommends 0.08 but direction is counterintuitive (lower cse is BETTER) — re-evaluate before enforcing | Operator review of cert verdicts | Walk-forward cert verdicts |
| 4 | Realized-EV trade rule + state-value guardrails | Structured in `live_ev_policy_runtime.py` (shadow) | Live fill-rate curves; paused during paper-only week | `data/analysis_output/ev_policy/` |
| 5 | Calibrated-edge stake scaling (Active #6 part 2) | Shadow; 11/30 sessions accumulated (2026-06-06) | 19 more live shadow sessions; paused during paper-only week | Weekly rollup → "Active #6 stake-scaling promotion" panel |
| 6 | Learned execution policy → replace fixed spread heuristic | Prototype refreshes daily; fill-gap cap (`--max-limit-gap-below-ask 0.02`) shipped 2026-06-04, untested live | ~200+ live bets; paused during paper-only week | `data/analysis_output/execution_policy_prototype/learned_execution_policy_report.md` |
| 7 | No-score drift promotion path | Paper-only (no-score walk-forward refreshes daily) | 60+ post-TR20 days of durable empirical-support ROI | `data/analysis_output/no_score_drift_walk_forward/` |
| 8 | Stage-1 Alt-A promotion (full enforce; TR25 scoped enforce already shipped) | Runtime-shadow promoted 2026-06-03; shadow report now 87% coverage / 10.2pp bias reduction. **Two open conflicts**: (a) inning≥8 `hold_poisson` rule contradicted by new evidence (+22.9pp improvement, n=19, 100% coverage); (b) fleet 2×2 warns scope-enforce + calibrator may double-shrink (see RF2) | RF2 verdict (B_cal_only conclusive ~2026-06-17) before expanding Alt-A | `data/analysis_output/stage1_shadow_override/` |
| 9–17 | (shipped 2026-05-17 → 2026-05-21) | ✅ Closed | — | [ROADMAP_ARCHIVE_2026_H1.md](ROADMAP_ARCHIVE_2026_H1.md) |

### Hygiene

| # | Item | Cost | Expected impact | Blocked by |
|---|---|---|---|---|
| 1 | Line-5.5 high-FV slice guard | One-line gate | **Largely superseded 2026-06-07** by the per-line calibrator (Hygiene #2 ship): the line-5.5 isotonic curve now corrects the slice directly (held-out gap −1.2pp, was +20-30pp). `K_line5p5_block` preset keeps running as live-fire corroboration (fleet paired-delta: TRENDING_POSITIVE). Decide retire-vs-promote when K reaches CONCLUSIVE. | Fleet paired-delta verdict on K |
| 2 | Mid-band [0.80, 0.90) calibrator under-confidence refit | **✅ Shipped 2026-06-07** as per-line calibrator stratification | `--per-line-min-rows 100` fits per-(family, line) Platt/isotonic curves; runtime prefers per-line, falls back pooled. Matched held-out eval: logloss −0.488 / brier −0.302 vs pooled; line 5.5 gap −1.2pp. Wired into daily refresh. OVER side only (UNDER per-line deferred — see #8). Closed. |
| 3 | Replace Poisson tail with negative-binomial fit | **🚧 Staging + fleet arm shipped 2026-06-11** | Builder mode `negative_binomial` (per-phase method-of-moments dispersion; non-overdispersed phases keep Poisson). First build: 402/480 phases overdispersed (mean var/mean = 2.20), and the high-FV acceptance check closed **93.4%** of the +10.7pp poisson-vs-empirical gap (900 well-supported pairs → +0.7pp residual). Daily refresh keeps `mlb_ou_cache_nb.staging.json` fresh; `O_nb_stage1` fleet arm runs it under the production calibrator. | ~2 weeks of O-vs-A paired-delta evidence; promotion decision alongside RF2 / Active #8 |
| 4 | Chain-rebuild stale-input artifacts | Investigate-and-reorder OR auto-chain-rebuild | **Partially closed 2026-06-03**: `rebuilt_each_refresh` classification suppresses transient stale alerts (daily review runs at step 14, before steps 17-38 rebuild). Remaining: promotion-gated artifact mismatches (stage3_v2 `phase4_models.json` hash; calibrator_over vs calibrator_under divergence on the shared training table) — these are real and surface correctly. | Operator promotes / rebuilds the gated artifacts |
| 5 | Gate-counterfactual cross-window validation | **✅ Shipped 2026-05-26** | `window_reversal` flag + `lifetime_post_calibrator_enforce` 4th window; confidence auto-downgrades to `review_required` on reversal. First run flagged 4 of 9 HIGH-confidence recs as reversed. Closed. |
| 6 | Fleet-root integration gap | Loader change in `unified_signal_table` (+ calibration-training builder) | **Discovered 2026-06-11**: the canonical learning loop (unified table → training table → walk-forward / loss-attribution / calibration) reads only `data/live_trading/*` + `data/paper_trading/*` hardcoded roots. Fleet engines write `data/paper_<label>/*` — their bets and candidate logs feed NOTHING canonical (only the aggregator + B4 + paired-delta blocks). Fleet evidence stays a parallel track until this closes. Decide: fold fleet rows in with a `config_label` column, or keep fleet deliberately quarantined and document that. | Design decision (quarantine vs fold-in) |
| 7 | Daily review hard-fails without a live-root session file | **✅ Shipped 2026-06-11** | Missing session now degrades instead of raising: session-dependent blocks publish empties, `session_missing: true` stamped on the report, `Session-missing:` warning leads the Notes feed, and the fleet/B4/artifact blocks publish regardless. 2 new tests. Closed. |
| 8 | UNDER per-line calibration | Re-run `--per-line-min-rows` for `--side under` once data supports it | **Deferred 2026-06-11 with evidence**: per-line UNDER is overfit on current counterfactual-label data (held-out logloss 0.813 vs 0.711 pooled; line-5.5 isotonic maps raw 0.05→0.82). Revisit when real UNDER paper outcomes accumulate (B4 runway). | B4 sample growth |

### Research findings

| # | Finding | Status | Most-actionable follow-up | Read |
|---|---|---|---|---|
| RF1 | Edge Atlas — market structurally overprices Over by **+2-5pp across every cohort** (lines 9.5/10.5/11.5 + inn_8-9 worst), validating bidirectional pivot premise | ✅ Shipped 2026-05-27 | RF1.a (recent-N comparison) tests whether 10y baseline is stale before any live use of the finding | `data/analysis_output/edge_atlas/edge_atlas.md` |
| RF1.a | Recent-N comparison: bias **survives directionally** across 3y/4y/5y/6y/10y windows (0 sign flips / 16 buckets), magnitude varies. Verdict: `BIAS_PARTIALLY_SURVIVES`. Bidirectional pivot premise confirmed regime-stable. | ✅ Shipped 2026-05-27 | RF1.b realized-outcomes deep dive (awaits more N), RF1.c per-park stratification | `data/analysis_output/edge_atlas/recent_n_comparison.md` |
| RF2 | Fleet 2×2 redundant-correction interaction — Alt-A scope-enforce **helps** with the calibrator OFF (D vs C: +$55 delta) but **hurts** with it ON (B vs A: +$79 against scope, 9/15 days): both levers shrink overconfident FV, and doing both double-shrinks past winners. Tracked automatically by the fleet paired-delta block; B_cal_only at t=1.71, ~6 days from CONCLUSIVE as of 2026-06-11 | 🔶 Pending verdict | Hold ALL Alt-A scope expansion (Active #8) until the verdict lands; if CONCLUSIVE_POSITIVE for B, the decision is scope-enforce OFF or calibrator-band rebalance, not more Alt-A | Daily review → `fleet_paired_delta_health` |

Last dashboard refresh: **2026-06-11**. Refresh after each ship.

---

Last roadmap review: **2026-06-11** (full-document audit during the
paper-only tuning week kickoff). Changes in this review: dashboard
refreshed for the first time since 05-27 (Active #1 now **READY** —
certified 2026-06-10 at 215 filled / 47 dates, ROI +5.1%, one verdict
already acted on); June ships backfilled into Recently Completed
(06-03 lever promotions + UNDER dedup fix, 06-04 phantom-risk gate +
fill-gap cap, 06-07 per-line calibrator + reconciler pass-2 — none of
which had ROADMAP entries); Hygiene #2 marked shipped (per-line
calibrator IS the mid-band refit), #1 marked superseded, #4 partially
closed; three new Hygiene items added (#6 fleet-root integration gap,
#7 daily-review missing-session guard, #8 UNDER per-line deferral);
new research finding RF2 (scope×calibrator redundant correction from
the fleet 2×2, verdict pending ~06-17); B4 status updated (4/60
sessions via the M fleet preset after the 06-10 scanner fix);
operational guidance gains the paper-only-week posture (fleet +
dry-run live engine for data continuity). Entries from 05-18→05-27
(~1,200 lines) moved to the archive per the trailing-7-days policy;
ROADMAP shrank 2,489 → ~1,300 lines.

Prior review: **2026-05-27** (RF1.a Recent-N Edge
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

- **Hygiene #3: negative-binomial Stage-1 tail — staging cache + two
  model-version fleet arms (+ Hygiene #7 guard)** *(2026-06-11,
  latest)* — the first work attacking the chronic +18pp raw-FV bias
  at its source instead of containing it downstream.

  **Builder** (`cache/build_mlb_ou_cache.py`): new
  `--smoothing-mode negative_binomial`. Pass 1 now accumulates
  remaining-runs sum-of-squares per phase; per-phase NB size `r` is
  fit by method of moments (`r = mean²/(var−mean)`) when the phase is
  overdispersed and has ≥ `--nb-min-phase-n` (default 200) samples;
  `poXX` comes from the NB survival function, with non-overdispersed
  / thin phases keeping Poisson (the NB cache is a strict superset).
  The pass-2 fallback-calibration table routes through the same
  distribution so its logit-deltas correct what the cells carry.
  Also fixed a latent mode-gate bug: `_apply_alt_a_smoothing`
  early-returned only on `poisson`, so any third mode would have had
  its values clobbered by empirical overrides.

  **First build (2021-2025 production window)**: **402/480 phases
  overdispersed, mean var/mean = 2.20** (Poisson assumes 1.00) —
  direct empirical confirmation of the audit thesis. Acceptance
  check on the high-FV danger zone (poXX ≥ 0.85, n_samples ≥ 100,
  900 cell×line pairs): production gap **+10.70pp** → NB gap
  **+0.70pp** (93.4% closed), achieved with ~2 moments per phase
  (~960 parameters constraining 25k+ cell values — structure, not
  memorization). Per line: 6.5/8.5/10.5 fully closed, 7.5 → +1.8pp,
  9.5 → +5.8pp (68% closed, thin n=33). Poisson-fallback cells
  byte-identical to production.

  **Fleet arms** (`launch_parallel_engines.py`, 11 → 13 presets):
  `O_nb_stage1` (NB staging cache) and `P_alt_a_cache` (full Alt-A
  staging cache — the exact promote.py candidate) — both are
  A_current + a `--cache-path` swap and nothing else, calibrator ON
  so the RF2 redundant-correction interaction is measured. The
  paired-delta block reads both vs A_current automatically. New
  `stage1_ou_cache_nb` refresh step keeps the staging cache fresh.

  **Hygiene #7 shipped in the same pass**: missing live-root session
  no longer kills the daily review — degrades with
  `session_missing: true` + a leading Notes warning while fleet/B4/
  artifact blocks publish normally.

  **Caveat recorded**: the 93% closure is in-corpus (the NB is fit on
  the same games the empirical rates come from). Out-of-sample
  validation is exactly what the O arm provides; promotion waits on
  ~2 weeks of paired-delta evidence + the RF2 verdict so the Stage-1
  decision (NB vs full Alt-A vs scope changes) is made once, with
  all options measured under the production calibrator.

  Tests: 8 new builder tests (method-of-moments math, scipy parity,
  correction direction, build integration, thin-phase fallback,
  Alt-A clobber guard), 2 fleet-preset tests, 2 session-guard tests.
  Full suite 1,561 unittest + 57 pytest green.

- **UNDER calibration re-enabled + fleet paired-delta daily-review
  block** *(2026-06-11)* — the two follow-ups from the
  2026-06-10 fleet audit.

  **1. UNDER calibration: M_under_paper flipped back to enforce.**
  The 2026-05-30 off-mode stop-gap produced honest volume but
  dishonest FVs: raw `1 - over_fv` is overconfident by construction
  (8 UNDER bets, 1W/7L, −69% ROI, calibration delta −39pp — B4 can
  never clear on that data). Evaluation of a per-line UNDER refit
  found it **overfit on current n** (held-out logloss 0.813 vs 0.711
  pooled; line-5.5 isotonic maps raw 0.05 → 0.82) — so per-line
  UNDER is deferred until real UNDER outcomes accumulate, and the
  pooled artifact stays. Two runtime fixes shipped with the flip:
  (a) `_maybe_emit_under_candidate` now routes the UNDER calibrator
  by the candidate's ACTUAL model family instead of hardcoding
  score_event_transition — the no_score_drift UNDER curve has real
  discrimination (0.20→0.62 spread) while score_event's is near-flat,
  so no-score candidates were being wasted on the flat curve;
  (b) `line=` passed through so per-line UNDER curves activate
  automatically if they ever ship. Engine startup warning updated to
  reflect the 2026-06-11 evaluation. Expect fewer but defensible
  UNDER paper bets (flat ~0.30 score_event FV clears gate_min_edge
  only on cheap asks).

  **2. Fleet paired-delta daily-review block**
  (`fleet_paired_delta_health`). New
  `human_review/fleet_health.py::_fleet_paired_delta_health` walks
  every `data/paper_<label>/sessions` root for the trailing 30d and
  computes, per preset vs `A_current`: shared/unique bet cohorts
  (key: date, game_pk, line, side, inning), unique-cohort W/L/P&L,
  `delta_net_pnl`, a Welch t on per-bet profit (one-sample fallback
  for strictly-additive presets like F_no_dedup), a verdict ladder
  (`NO_SHARED_DAYS → DEAD → CONCLUSIVE_± → TRENDING_± → COLLECTING`),
  and a `days_to_significance` extrapolation. CONCLUSIVE and DEAD
  verdicts mirror to Notes via `Fleet-delta:` prefix. First
  production run reproduces the 2026-06-10 manual audit exactly:
  N_extreme_edge_022 → DEAD (alert fired), B_cal_only →
  TRENDING_POSITIVE (t=1.71, ~6 days to verdict), F_no_dedup →
  TRENDING_POSITIVE (t=1.43, ~14 days). The next fleet conclusion
  surfaces in the daily review without a manual audit. 19 new tests;
  full suite 1,558 green.

- **Fleet prune + paired-delta audit quick wins** *(2026-06-10)* —
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

- **Per-line calibrator stratification + orphan-fill reconciler pass 2**
  *(2026-06-07, backfilled 2026-06-11)* — two ships that closed audit
  findings from the 06-06 two-week WR-drop investigation.
  1. **Per-line calibrator (closes Hygiene #2).** New
     `--per-line-min-rows` flag on `calibrate_signal_probabilities.py`
     fits an additional Platt/isotonic curve per (model_family, line)
     when the line has ≥100 labeled rows; artifact stores them under
     `families[<fam>][lines][<line>]`; runtime
     (`probability_calibration.py`) prefers the per-line curve and
     falls back to family-pooled (strictly additive — legacy artifacts
     unchanged). Matched held-out eval vs pooled on n=420:
     **logloss −0.488, brier −0.302**; the chronic line-5.5 slice goes
     from +20-30pp gap to **−1.2pp**. Daily refresh passes
     `--per-line-min-rows 100`; `diag.calibrator_scope` records
     per_line vs family_pooled per tick. 13 curves in the first
     production artifact (94% row coverage). 8 new tests.
  2. **Reconciler pass 2 (losing-side orphan fills).** The 2026-06-06
     CWS@PHI O11.5 incident: ask dropped below our limit, the fill and
     the ask-reversal cancel raced, the bot recorded `cancelled` while
     Polymarket filled — and the position-based reconciler was
     structurally blind because losing shares resolve to $0 and vanish
     from the wallet. New trade-history pass in
     `live_reconciliation.py` queries the data-api `/trades` per
     cancelled candidate after the position pass; new
     `data_api_trades_only` source tag + `orphans_trade_only` counter.
     6 new tests including a regression for the exact incident.

- **gate_phantom_risk_band + execution fill-gap cap + counterfactual
  dedup fixes** *(2026-06-04, backfilled 2026-06-11)* —
  1. **New enforced gate `gate_phantom_risk_band`** (Gate 8f.4,
     `signal_pipeline_gates_post_fv.py`): blocks candidates with
     `shadow_phantom_risk_score >= 0.70` (the high band). Cohort
     evidence: n=58 since 05-01, WR 56.9%, ROI −13.9%, Wilson 95% CI
     entirely below break-even. CLI `--max-phantom-risk-score`;
     routed through the override file
     (`gate_thresholds.gate_phantom_risk_band`); activated in
     `cache/live_engine_overrides.json` 2026-06-06.
  2. **`--max-limit-gap-below-ask` (default 0.02)** caps placement
     limit at ask−2¢. Fill audit: 27 of 28 cancelled orders priced
     1-11¢ below ask would have WON (~$5-7/day foregone). Live-only;
     untested until live re-entry.
  3. **Calibrator-enforce counterfactual dedup**: blocked tick-rows
     deduped by (game_pk, line, side) — the "-$1k/day blocked" claim
     was 533 rows describing 15 opportunities (35.5× inflation). Same
     fix mirrored in the UNDER outcomes counterfactual. New
     `same_game_multi_fire_health` block distinguishes tight
     (dedup-leak signature) vs loose multi-fires.
  4. **`scrape_mlb_history` atomic-rename retry** (5 attempts,
     exponential backoff) fixes the Windows file-lock failure when the
     live engine holds a schedule file open during refresh.

- **UNDER dedup leak fix + Alt-A runtime-shadow + gate_extreme_edge
  retune** *(2026-06-03, backfilled 2026-06-11)* — three lever ships
  from the profitability deep-dive.
  1. **UNDER dedup leak**: the UNDER paper path had ZERO dedup state
     (5× TEX@STL U10.5 fired in 17s on 06-02). Added the four parallel
     UNDER dedup dicts in `signal_engine.py` + checks in
     `_maybe_emit_under_candidate` + session-resume routing. 3 tests.
  2. **Alt-A promoted to runtime-shadow** via the new
     `stage1_shadow_empirical_mode` override-file route — computes
     `fair_value_alt_empirical` per tick so the scoped-enforce (live
     since 05-22) consumes runtime evidence. Audit row in
     `promotion_events.jsonl`.
  3. **`gate_extreme_edge` 0.22 → 0.30** per the walk-forward RETUNE
     verdict (blocked cohort ROI beat kept by +9pp); shipped via the
     override file; fast Wilson-UB demote armed. Same day: daemon
     readiness reworded with per-lever blocker reasons;
     cross-artifact transient-stale suppression
     (`rebuilt_each_refresh` 3-tuple) closed most of Hygiene #4's
     alert noise.

<!-- Older entries (2026-05-27 and earlier) have been archived. -->
<!-- See [ROADMAP_ARCHIVE_2026_H1.md](ROADMAP_ARCHIVE_2026_H1.md). -->

_For shipped work from 2026-05-27 and earlier, see_ **[ROADMAP_ARCHIVE_2026_H1.md](ROADMAP_ARCHIVE_2026_H1.md)**.


## Active priorities

1. **Walk-forward certification — ✅ READY achieved 2026-06-10, exactly
   on the predicted date.** 215 filled bets / 47 session dates
   (thresholds 150/30), overall ROI +5.1%, filled WR 67.0%, max DD
   −$541. The enforced gate stack is now certified against the live
   model. Verdict ledger:
   - **Acted on**: `gate_extreme_edge` 0.22→0.30 (RETUNE, medium
     confidence; blocked cohort ROI beat kept by +9pp) — promoted
     2026-06-03 via the override file; fast Wilson-UB demote armed
     (paused during the paper-only week, no live fills to evaluate).
   - **Open RETUNE**: `gate_high_line_min_inning` 5→6 (low confidence,
     +18.3pp blocked-vs-kept; n=6 blocked — thin).
   - **Open EXPLORE**: `shadow_gate_current_state_edge_min` — sweep
     best 0.08, but the direction is counterintuitive (lower cse
     performs BETTER); see Active #3 before enforcing anything.
   - All other gates: KEEP (most at low confidence due to thin blocked
     cohorts — the gates rarely fire, which is itself evidence they
     sit at sane thresholds).
   - **Known display gap**: the cert sweeps from `signal_config`
     defaults, so it shows `gate_extreme_edge current=0.22` even
     though live runs 0.30 via the override file. Read the override
     file as truth for "current".
   - Files: `scripts/analysis/build_walk_forward_certification.py`,
     `walk_forward_runner.py`, `build_signal_training_table.py`.

2. **Watch the orphan-fill reconciler and decide whether the data-api
   path becomes primary.** The reconciler is fail-open; we need to
   measure how often it fires vs. the CLOB SDK's normal fill path. If
   reconciliation is recovering >10% of real fills, the SDK path is the
   exception, not the rule, and the live engine should query the data-api
   first instead of as a safety net.
   - **Pass 2 shipped 2026-06-07** after the CWS@PHI O11.5 incident
     (cancel/fill race; position-based pass structurally blind to
     LOSING orphans because resolved-worthless shares vanish from the
     wallet). The reconciler now runs a trade-history pass per
     cancelled candidate; new `data_api_trades_only` source tag +
     `orphans_trade_only` counter flow into the daily-review
     `by_source` breakdown automatically.
   - Tracking is automated: the daily human-review report carries
     an "Orphan-Fill Reconciler" section with per-source counts and
     fires an alert when recovered_share >= 10% (with min sample 3
     filled).
   - **Paused during the paper-only week** (no live orders to
     reconcile); the ~30-session evidence clock resumes on live
     re-entry. The 2026-06-06 incident itself (~$20 invisible loss) is
     still un-backfilled in the ledger — one-shot backfill script is
     an open offer.
   - Promote primary-source change only with a written justification
     across ~30 sessions of evidence.
   - Files: `scripts/trading/live_reconciliation.py`,
     `polymarket_client.py`, `live_order_lifecycle.py`.

3. **Promote shadow signals to enforced gates -- only with walk-forward
   support.** *Unblocked 2026-06-10 (Active #1 READY) — but the
   certified evidence flipped the original thesis on one candidate:*
   - `current_state_edge_min >= 0.05` — **the cert contradicts the
     pre-TR20 read.** The EXPLORE sweep shows lower cse performs
     BETTER (cse_<0.03 = +14.9% ROI vs cse_>=0.08 = −9.0%); the sweep's
     "best" 0.08 threshold would block the profitable cohort. Do NOT
     enforce the original >=0.05 design; if anything the gate inverts.
     Needs a re-design pass, not a promotion.
   - `gate_ask_max=0.85` — cert's >=0.80 ask cohort shows −2.7% ROI on
     n=18 filled (thin). Hold for more sample; the Winner's-Curse
     evidence clock pauses during the paper-only week (paper has no
     fill selection).
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
   **Status 2026-06-11**: 11/30 shadow sessions accumulated (per the
   06-06 daemon-readiness block); the clock pauses during the
   paper-only week because the verdict reads live filled bets.

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
   - **2026-06-04 partial ship**: `--max-limit-gap-below-ask` (default
     0.02) caps the placement limit at ask−2¢ — the fill audit found
     27 of 28 cancelled orders priced 1-11¢ below ask would have won
     (~$5-7/day foregone). This is the cheapest slice of the execution
     policy, shipped ahead of the learned lookup. **Untested live**
     (flag is live-only and the paper-only week has no order
     lifecycle); first live session with the flag is the test.
   - **Paused during the paper-only week**: the ~200-bet sample clock
     needs live fills. The previous ~2026-06-15 estimate slips by
     roughly the length of the paper week.
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
   Poisson smoothing". Ladder so far: shadow-override report (-6pp,
   05-17) → staging cache (05-18) → scoped enforce with inning≥8
   held to Poisson (05-22) → **runtime-shadow promoted 2026-06-03**
   via the `stage1_shadow_empirical_mode` override-file route.
   Latest shadow evidence (06-06 review): **87% coverage, 10.2pp
   aggregate bias reduction** (+17.9pp → +7.7pp on 107/123 bets).
   The ENFORCE flip remains one CLI command:
   `python scripts/analysis/promote.py stage1 --source
   cache/mlb_ou_cache_alt_a.staging.json`.

   **HOLD — two open conflicts must resolve first (2026-06-11):**
   - **(a) The inning≥8 `hold_poisson` rule is now contradicted.** It
     was added 05-22 on a −23.8pp regression (n=7). The 06-06 shadow
     report shows the same cohort now IMPROVING +22.9pp under Alt-A
     (n=19, 100% coverage). The scope rule is backwards per current
     evidence — revisit it in the same pass as any expansion.
   - **(b) RF2 redundant correction.** The fleet 2×2 shows
     scope-enforce HELPS with the calibrator off but HURTS with it on
     (A's uniques lost to B's by $79; both levers shrink overconfident
     FV, double-shrinking past winners). With the per-line calibrator
     (06-07) making the calibrator stronger, expanding Alt-A may
     over-correct further. Wait for the B_cal_only paired-delta
     verdict (~2026-06-17) — if CONCLUSIVE_POSITIVE, the right move
     may be scope-enforce OFF + full Alt-A cache + lighter calibrator
     band, not incremental scope expansion.
   - A clean test exists with zero new code: a fleet arm running
     `--cache-path cache/mlb_ou_cache_alt_a.staging.json` (the full
     Alt-A cache as Stage-1) with calibrator ON — live-fire evidence
     for the exact promotion candidate.
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
    **Status update 2026-06-11: largely superseded.** The per-line
    calibrator (Hygiene #2 ship, 06-07) corrects the line-5.5 slice
    at the FV level (held-out gap −1.2pp, was +20-30pp), making the
    hard block mostly redundant. K preset keeps running as
    corroboration (fleet paired-delta: TRENDING_POSITIVE, t=1.68);
    when it reaches a verdict, decide retire (calibrator suffices)
    vs promote (belt-and-suspenders on the worst slice).

2. **Mid-band [0.80,0.90) calibrator under-confidence
    refit.** *✅ Shipped 2026-06-07 as per-line calibrator
    stratification.* The 2026-05-19 audit found the global Platt
    pulls too aggressively in [0.80,0.90) — raw +5-9pp overconfident
    vs realized but calibrated 10-16pp UNDER realized. The fix
    shipped exactly as specced here ("per-line + per-family would
    let the band reach a tighter local fit; or swap to isotonic"):
    `--per-line-min-rows 100` fits per-(family, line) curves, and
    the method selector picked isotonic precisely where Platt
    over-pulled (score_event lines 5.5/6.5/7.5/10.5). Matched
    held-out eval: logloss −0.488 / brier −0.302 vs pooled. Wired
    into the daily refresh; runtime falls back to family-pooled for
    lines under the row floor. OVER side only — the UNDER mirror is
    deferred with evidence (see Hygiene #8). Files (shipped):
    `scripts/analysis/calibrate_signal_probabilities.py`,
    `scripts/trading/probability_calibration.py`,
    `scripts/trading/signal_engine.py`,
    `scripts/analysis/refresh/steps/_canonical_tables.py`.

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
    change than the cheap wins. **Status update 2026-06-11: now the
    primary raw-model fix.** The 2026-06-06 Stage-1 retrain
    experiment proved the cells are NOT stale (adding 2026 data
    moved cell FVs <0.2pp; 2026 league runs/game = 8.91 is
    dead-center of the training regime) — the chronic +18pp raw
    bias lives in the Poisson smoothing/tail, exactly this item.
    Sequencing: resolve the Active #8 / RF2 Alt-A decision first
    (Alt-A's empirical-when-available and a neg-binomial tail
    overlap on the same fix surface; shipping both blind would
    re-create the redundant-correction problem RF2 found between
    scope and the calibrator). Files: `cache/build_mlb_ou_cache.py`.

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
    **Status update 2026-06-11: partially closed 2026-06-03.** The
    transient-stale class of alerts is gone:
    `CROSS_ARTIFACT_CONSISTENCY_PATHS` extended to a 3-tuple with
    `rebuilt_each_refresh`, and the health block suppresses (but
    records under `suppressed_transient_stale`) artifacts the same
    refresh will rebuild anyway. Remaining open: the
    promotion-gated mismatches the block correctly keeps raising —
    stage3_v2 weights' recorded `phase4_models.json` hash vs current,
    and the calibrator_over vs calibrator_under divergence on the
    shared training table (one is built before the table updates,
    one after). These clear when the operator promotes/rebuilds the
    gated artifacts, not via refresh ordering.

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

6. **Fleet-root integration gap.** *Discovered during the 2026-06-11
    paper-only-week wiring audit.* The canonical learning loop reads
    two hardcoded root pairs — `data/live_trading/*` and
    `data/paper_trading/*` (`build_unified_signal_table.py:87-95`,
    `build_calibration_opportunity_training_table.py`) — while the
    parallel-engine fleet writes per-preset roots
    (`data/paper_<label>/*`, see `candidate_paths.py`). Consequence:
    **fleet bets and fleet candidate logs feed nothing canonical** —
    not the unified table, training table, walk-forward cert,
    loss-attribution, or calibration. Fleet evidence lives on a
    parallel track consumed only by `aggregate_parallel_engines.py`,
    the B4 milestone (fleet roots added 2026-06-10), and the
    paired-delta block (2026-06-11). Two design directions:
    (a) **Fold in**: extend the loaders to glob `data/paper_*/`
        with a `config_label` column so downstream consumers can
        filter; canonical metrics would need care not to
        double-count 11 copies of the same signal.
    (b) **Quarantine deliberately** (status quo, documented): the
        fleet is an A/B instrument, not training data; keep the
        canonical loop fed by live + dry-run live engine only.
    Leaning (b) — folding 11 correlated copies of each game into
    training/calibration tables creates a pseudo-replication
    problem worse than the missing data. Decide explicitly rather
    than by accident. Files: `scripts/analysis/unified_signal_table/`,
    `build_calibration_opportunity_training_table.py`.

7. **Daily review hard-fails when the live-root session file is
    missing.** `build_daily_human_review_report.py` loads
    `sessions_dir/<date>_session.json` via a raising `_load_json`
    (~line 320). A day with no live-root session (crash, operator
    skip, pure-fleet day) kills the ENTIRE daily review — including
    the fleet paired-delta, B4, and artifact-health blocks that
    don't need a live session at all. Mitigated during the
    paper-only week by running the dry-run live engine; proper fix
    is a missing-file guard that degrades session-dependent blocks
    to a `no_session` status while the rest of the report publishes.
    ~1h. Files: `scripts/analysis/build_daily_human_review_report.py`.

8. **UNDER per-line calibration (deferred with evidence).** The
    2026-06-11 evaluation ran `--side under --per-line-min-rows 100`
    and found per-line UNDER **overfit on current data**: matched
    held-out logloss 0.813 vs 0.711 pooled, with the line-5.5
    isotonic mapping raw 0.05 → 0.82 (would fire terrible bets).
    Root cause: UNDER training labels are counterfactuals derived
    from OVER outcomes whose raws concentrate in [0, 0.1] — there is
    no real discrimination for per-line curves to learn yet. Revisit
    once actual UNDER paper outcomes accumulate on the B4 runway
    (M preset, calibration enforce as of 06-11). The runtime is
    already wired (`line=` passed through
    `_maybe_emit_under_candidate`; per-line curves activate
    automatically if the artifact ever carries them). Files:
    `scripts/analysis/calibrate_signal_probabilities.py`,
    `scripts/trading/signal_pipeline.py`.

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

### RF2. Scope × calibrator redundant correction (2026-06-10, verdict pending)

**Output**: daily review → `fleet_paired_delta_health` block (tracks
the verdict automatically); discovered in the 2026-06-10 manual
paired-delta fleet audit.

**Method**: the fleet's A/B/C/D presets form a deliberate 2×2
factorial over (prob-calibration enforce/off) × (Alt-A scope-enforce
on/off). The paired-delta lens — comparing the bets each config
takes/skips vs its control on shared days, not marginal ROI tables —
decomposes the scope effect in each calibrator cell.

**Finding**: the scope effect FLIPS SIGN depending on the calibrator.
- Calibrator OFF (D_scope_only vs C_raw): scope-enforce **helps** —
  C without scope takes 80 extra bets that lose −$66; delta +$55
  for scope.
- Calibrator ON (B_cal_only vs A_current): scope-enforce **hurts** —
  A (scope on) skipped 52 bets that won at 73% while B kept them;
  delta +$79 against scope, B ahead on 9 of 15 days, Welch t = 1.71.

**Interpretation**: Alt-A scope-enforce and the Platt calibrator are
*redundant corrections* — both shrink overconfident raw FV. Either
alone helps; both together double-shrink, pushing real winners below
the edge threshold. Coherent with the calibrator's known strength
(mean |cal−raw| ≈ 24pp in-band) and now strengthened by the
per-line calibrator ship (2026-06-07).

**Decision pending**: B_cal_only sits at TRENDING_POSITIVE
(t = 1.71, ~6 days to significance as of 2026-06-11). When the
`Fleet-delta:` CONCLUSIVE alert fires, the decision is between
(a) scope-enforce OFF in production (calibrator does the shrinking),
(b) calibrator band-gate raised + scope kept (L_enforce_min_raw_095
arm informs this), or (c) full Alt-A cache + lighter calibrator.
**Until then, Active #8's Alt-A expansion is HELD.**

**Caveat**: the fleet runs paper fills; the B-vs-A delta cohort is
FV-level (transfers to live well per the execution-sensitivity
ranking in the 2026-06-11 paper-week notes), but confirm on live
re-entry before any production flip.

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

B4. **UNDER paper-mode validation period.** *Clock running:
    4/60 sessions as of 2026-06-10.* Evidence accumulates via the
    `M_under_paper` fleet preset (running `--under-mode paper`
    daily since 2026-05-30). Status notes:
    - **DONE**: Phase C-paper ships `--under-mode paper` + the 5
      symmetric UNDER gate stack (extreme_edge, fv_ask_gap,
      max_base_fv, min_inning, min_entry_ask).
    - **DONE**: B4 milestone dashboard shipped
      (`_under_paper_b4_milestone_health`, verdict ladder
      `NOT_EMITTING → INSUFFICIENT_SESSIONS → ... → READY`).
    - **DONE 2026-06-10**: B4 scanner blind spot fixed — the block
      now also walks fleet roots (`B4_EXTRA_PAPER_SESSION_ROOTS`);
      before the fix it read 0/60 while M accumulated invisibly.
    - **DONE 2026-06-11**: M flipped back to
      `--under-calibration-mode enforce` (off-mode produced honest
      volume but dishonest FVs: 1W/7L, −39pp calibration delta —
      could never clear B4's ROI/calibration conditions). Expect
      fewer but defensible UNDER paper bets; the early-window
      numbers (taker ROI −69%) are dominated by pre-fix off-mode
      bets + the 06-02 dedup-leak multi-fires and will dilute as
      enforce-mode sessions accumulate.
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

- **CURRENT POSTURE (2026-06-11 → ~2026-06-18): paper-only tuning
  week.** No real-money orders; focus is model tuning. Two processes
  run daily:
  `python scripts/trading/launch_parallel_engines.py` (11-preset
  fleet; owns the daily refresh) AND
  `python scripts/trading/live_engine.py --dry-run
  --no-startup-refresh` (keeps the live-root candidate universe
  flowing — the calibration training table, daily review, drift
  blocks, and B4/fleet blocks all depend on a live-root session
  existing; see Hygiene #6/#7 for why). Standing caveats for the
  week: paper fills at ask include bets live would cancel/miss, so
  hold execution-sensitive fleet verdicts (F_no_dedup especially,
  H_late_innings, M_under_paper) to a higher bar than FV-level ones;
  expect cosmetic zero-fill drift alerts; the live-fill evidence
  clocks (Active #2/#4/#5/#6, fast-demote on the 06-03 promotion)
  pause until re-entry. On re-entry: treat the first live days as
  re-validation of paper-tuned changes, and add
  `--max-limit-gap-below-ask 0.02` to the live command (first live
  test of the 06-04 fill-gap cap).
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
