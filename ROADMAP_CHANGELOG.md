# Roadmap Changelog

Rolling history carved out of **[ROADMAP.md](ROADMAP.md)** on 2026-06-13 to
keep the active document scannable (the same move that produced
[ROADMAP_ARCHIVE_2026_H1.md](ROADMAP_ARCHIVE_2026_H1.md) on 2026-05-25).
Two parts:

- **Review log** — dated roadmap-review notes, newest first. ROADMAP.md
  keeps only its `Last reviewed:` pointer; the detail lands here.
- **Recently completed (archived)** — shipped-work entries that aged out
  of the trailing window ROADMAP.md keeps inline.

Shipped work from 2026-05-27 and earlier lives in
**[ROADMAP_ARCHIVE_2026_H1.md](ROADMAP_ARCHIVE_2026_H1.md)**. When this file
spans a half-year, cut its oldest entries into a dated archive of the same
form.

## Review log

Last roadmap review: **2026-06-15** (paper-window optimization sprint +
cert fix). **Shipped** (all safe / offline / diagnostic — no trading
behaviour changed): **T1** shadow-CLV collector
(`build_shadow_clv.py` + daily-review block) — its first run answered the
session's headline question: the selection residual is **ADVERSE_SELECTION**
(≈62% of losses drift away from us within 2 min = market-knew, vs 13% flat
= model-wrong). **T2** fleet-root gap closed as a *guarded fold-in* (Hygiene
#6): `--include-fleet` emits a SEPARATE labeled `unified_signals_fleet/`
artifact; canonical `signals_master` byte-identical / never pooled. **T3**
new `Q_max_base_fv_095` fleet arm; **T4** retired `H_late_innings` +
`I_extreme_018` (trending -EV, not concluded — accurate comments);
**T5** B4 marked **DORMANT** (limiter is UNDER signal quality, not session
count); **T6** execution-sensitivity tags on paired-delta; **T7**
concept-drift PSI watchpoint (one-shot, auto-re-arming). **T9** (market-
anchored-alpha runtime shadow) **QUEUED** as Hygiene #11 + a ship spec
(`docs/operational/market-anchored-alpha-runtime-shadow.md`) — it unblocks
the T8 fleet arm; held for sign-off (live signal pipeline). **Cert verdict
blind spot fixed**: `_gate_verdict` now runs a sweep-aware *tighten* check
before the current-blocked-N early-return, so a loose gate that blocks ~0
today still RETUNEs when a tighter sweep threshold blocks a materially -EV
cohort. Re-run surfaced **8 tighten RETUNEs** the old logic missed —
`gate_max_base_fv`→0.95 (de-risks Hygiene #10: the cert no longer says KEEP)
plus several OVERLAPPING early-inning/low-total gates (don't sum). **Then
brought the Hygiene #5 window-reversal guard INTO the cert tighten verdict**
(recompute the blocked cohort on the trailing half of session-dates;
three-state durable / window_reversal / unvalidated). It immediately caught
`gate_min_current_total`→6 as a **reversal** (full window −12.5% but recent
half **+4.5%** on n=57 — the exact 4→5 lifetime-reversal the audit flagged),
plus `runs_needed_max` and `fv_ask_gap_max`, all downgraded to
`review_required`; only `gate_max_base_fv`→0.95 and `gate_min_inning`→6 stay
DURABLE @ medium (both match the gate_counterfactual). So the cert's tighten
verdicts now self-validate instead of surfacing in-sample mirages at face
value. Full suite 1768 passed. Prior review **2026-06-14** below.

Last roadmap review: **2026-06-14** (week-log audit of 06-07→06-13, plus
acting on the resulting task list). **Findings:** (1) Calibrator-enforce
blocked net-winning would-bets all 7 days (≈−$94 cumulative would-block
counterfactual); the `calibration_edge_shaving` deep dive shows the
[0.95,1.0) band is correctly blocked (−17% ROI) while [0.90,0.95) is
~breakeven — recommend `enforce_min_raw` 0.90→0.95 (new Hygiene #9), held
for `L_enforce_min_raw_095` to firm up + sign-off. (2) `gate_max_base_fv`
0.99→0.95 recommended (new Hygiene #10): gate_counterfactual +$73/30d /
+$152 lifetime / +$93 post-cal, no window-reversal, and the cert's own
sweep agrees (kept ROI +4.85%→+11.6%); the cert's `KEEP` is a heuristic
artifact that only inspects the current threshold. (3) **Live-root gap
06-07→06-10** — real-money fills stopped after 06-06 and the dry-run
continuity engine didn't start until 06-11, so concept-drift PSI ran on 8
rows (`insufficient_data`) exactly when the calibrator alert needed it; new
runbook `docs/operational/live-pause-continuity.md`. (4) Stage-1 decision
**slipped ~06-17 → ~late-July**: the paper-week volume drop pushed
`B_cal_only` from t=1.71/~6d (06-11) to t=1.09/~45d (06-13) and the O/P
Stage-1 arms have only 2 days of data. (5) `stage3_v2_weights` stale 25d +
`phase4_models.json` hash mismatch — benign (daemon: research==active at
9e-05) but recommend `promote.py stage3-v2` to clear lineage. (6)
no_score_drift calibrator method flip-flop is a symptom of the live-data
gap freezing the selection history, not a missing gate (the modal stability
gate already exists). **Shipped this pass** (diagnostics only, no trading
behavior changed): calibrator-enforce blocked-outcomes split by raw-FV band
(`by_raw_fv_band` + band-aware muting-winners alert; the daily review now
distinguishes the toxic tail from the muted breakeven band); retired fleet
presets (E/G/N) excluded from the paired-delta block so concluded
experiments stop emitting stale DEAD alerts. Full suite green (282
daily-review + 20 fleet). Prior review **2026-06-11** below.

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

## Recently completed (archived from ROADMAP.md)

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

Shipped work from 2026-05-27 and earlier: **[ROADMAP_ARCHIVE_2026_H1.md](ROADMAP_ARCHIVE_2026_H1.md)**.
