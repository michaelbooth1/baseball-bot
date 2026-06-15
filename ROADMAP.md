# Roadmap To The State-Value Objective

**Companion top-level docs:**
- **[README.md](README.md)** — operator overview, gate stack, evidence
  snapshot, CLI usage. Read this for the *what* and *why* before touching
  code.
- **[MASTER_CONTEXT.md](MASTER_CONTEXT.md)** — repo map; one entry per
  top-level folder pointing at its AGENT_CONTEXT.md. Read this when you
  need to know where something lives.
- **[ROADMAP_CHANGELOG.md](ROADMAP_CHANGELOG.md)** — rolling history:
  dated roadmap-review notes plus shipped-work entries older than the
  trailing window kept below.
- **[ROADMAP_MARKET_MAKER.md](ROADMAP_MARKET_MAKER.md)** — the
  long-horizon bidirectional → market-maker pivot (Phases A–E).
  Strategic reference, not a per-session surface.
- **[ROADMAP_ARCHIVE_2026_H1.md](ROADMAP_ARCHIVE_2026_H1.md)** — shipped
  work from 2026-05-17 and earlier (~80 entries).

This file is the live operating surface — what to do next. It holds a
triage dashboard plus five sections:

- **Recently Completed** — the trailing few ship-days only; older entries
  move to **[ROADMAP_CHANGELOG.md](ROADMAP_CHANGELOG.md)**.
- **Active Priorities** — ordered by what most needs to happen next, given
  the post-TR20/TR21 reality (Stage-3 v2 + Stage-2 density_alt/hr_factor).
- **Hygiene** — accumulating debt; not blocking, but worth closing on a
  regular cadence so the loop stays clean.
- **Research findings** — long-form analysis outputs and the deeper-dive
  follow-ups they suggest. Distinct from Hygiene because each item is a
  research question (conditioned on more data or further analysis), not a
  known shipping action.
- **Operational guidance** — standing rules for day-to-day session work.

The long-horizon bidirectional → market-maker pivot now lives in
**[ROADMAP_MARKET_MAKER.md](ROADMAP_MARKET_MAKER.md)**.

**Maintenance:** roadmap-review notes go to
[ROADMAP_CHANGELOG.md](ROADMAP_CHANGELOG.md) — this file keeps only the
`Last reviewed:` line below. Recently Completed is capped to the trailing
~7 days; cut overflow to the changelog on each review.

## Verdict status dashboard

One-line status per Active and Hygiene item, with the artifact path the
operator should read next. Use this to triage; full text is below.

### Active priorities

| # | Item | Status | Blocked by | Read |
|---|---|---|---|---|
| 1 | Post-TR20+TR21 walk-forward + re-certify gates | ✅ **READY 2026-06-10** (215 filled / 47 dates, ROI +5.1%). First verdict acted on: `gate_extreme_edge` 0.22→0.30 promoted 2026-06-03. **2026-06-15: cert verdict blind spot fixed** (sweep-aware tighten check) **+ window-reversal guard** (recent-half recompute, mirrors Hygiene #5). Of 8 surfaced tighten RETUNEs: **2 DURABLE @ medium** — `gate_max_base_fv`→0.95 and `gate_min_inning`→6 (recent half still −EV; both match the gate_counterfactual) — these are the actionable ones; **3 flagged WINDOW-REVERSAL @ review_required** — `min_current_total`→6 (recent +4.5%!), `runs_needed_max`→2.5, `fv_ask_gap_max`→0.24 (in-sample/decaying — do NOT act); 3 low/unvalidated. Overlapping cohorts — don't sum. | Promote the 2 durable on live re-entry; ignore the reversed | `data/analysis_output/walk_forward_certification/walk_forward_certification.md` |
| 2 | Orphan-fill reconciler — primary vs safety-net | Pass-2 (losing-side trade-history recovery) shipped 2026-06-07 after the CWS@PHI 11.5 race-condition incident; watching | Paused during paper-only week (no live fills); resume evidence clock on live re-entry | Daily review "Orphan-Fill Reconciler" section |
| 3 | Shadow → enforce gate promotions (`gate_ask_max=0.85`, `current_state_edge_min`) | **Unblocked** (Active #1 READY). Cert's cse sweep recommends 0.08 but direction is counterintuitive (lower cse is BETTER) — re-evaluate before enforcing | Operator review of cert verdicts | Walk-forward cert verdicts |
| 4 | Realized-EV trade rule + state-value guardrails | Structured in `live_ev_policy_runtime.py` (shadow) | Live fill-rate curves; paused during paper-only week | `data/analysis_output/ev_policy/` |
| 5 | Calibrated-edge stake scaling (Active #6 part 2) | Shadow; 11/30 sessions accumulated (2026-06-06) | 19 more live shadow sessions; paused during paper-only week | Weekly rollup → "Active #6 stake-scaling promotion" panel |
| 6 | Learned execution policy → replace fixed spread heuristic | Prototype refreshes daily; fill-gap cap (`--max-limit-gap-below-ask 0.02`) shipped 2026-06-04, untested live | ~200+ live bets; paused during paper-only week | `data/analysis_output/execution_policy_prototype/learned_execution_policy_report.md` |
| 7 | No-score drift promotion path | Paper-only (no-score walk-forward refreshes daily) | 60+ post-TR20 days of durable empirical-support ROI | `data/analysis_output/no_score_drift_walk_forward/` |
| 8 | Stage-1 Alt-A promotion (full enforce; TR25 scoped enforce already shipped) | Runtime-shadow promoted 2026-06-03; shadow report now 87% coverage / 10.2pp bias reduction. **Two open conflicts**: (a) inning≥8 `hold_poisson` rule contradicted by new evidence (+22.9pp improvement, n=19, 100% coverage); (b) fleet 2×2 warns scope-enforce + calibrator may double-shrink (see RF2) | RF2 verdict (B_cal_only conclusive **~late-July at current pace, slipped from ~06-17** — O/P arms only 2 days in, B_cal_only ~45d to significance per the 06-14 audit) before expanding Alt-A | `data/analysis_output/stage1_shadow_override/` |
| 9–17 | (shipped 2026-05-17 → 2026-05-21) | ✅ Closed | — | [ROADMAP_ARCHIVE_2026_H1.md](ROADMAP_ARCHIVE_2026_H1.md) |

### Hygiene

| # | Item | Cost | Expected impact | Blocked by |
|---|---|---|---|---|
| 1 | Line-5.5 high-FV slice guard | One-line gate | **Largely superseded 2026-06-07** by the per-line calibrator (Hygiene #2 ship): the line-5.5 isotonic curve now corrects the slice directly (held-out gap −1.2pp, was +20-30pp). `K_line5p5_block` preset keeps running as live-fire corroboration (fleet paired-delta: TRENDING_POSITIVE). Decide retire-vs-promote when K reaches CONCLUSIVE. | Fleet paired-delta verdict on K |
| 2 | Mid-band [0.80, 0.90) calibrator under-confidence refit | **✅ Shipped 2026-06-07** as per-line calibrator stratification | `--per-line-min-rows 100` fits per-(family, line) Platt/isotonic curves; runtime prefers per-line, falls back pooled. Matched held-out eval: logloss −0.488 / brier −0.302 vs pooled; line 5.5 gap −1.2pp. Wired into daily refresh. OVER side only (UNDER per-line deferred — see #8). Closed. |
| 3 | Replace Poisson tail with negative-binomial fit | **🚧 Staging + fleet arm + out-of-sample replay shipped 2026-06-11** | Builder mode `negative_binomial`: 402/480 phases overdispersed (mean var/mean = 2.20); in-corpus high-FV gap closed 93.4%. **Out-of-sample replay on 1,327 settled 2026 candidates** (`stage1_nb_replay`): raw-chain bias +25.1pp → **+16.7pp**, brier 0.287 → 0.257 — real but partial (~33% of realized bias closed; the in-corpus 93% does NOT transfer). **NB ≈ Alt-A out-of-sample** (Alt-A +15.6pp, brier 0.257): they correct the same phenomenon. Residual ~16pp is selection-driven (bet-conditional), needing a market/selection-aware lever, not better smoothing. `O_nb_stage1` arm provides the live-fire + calibrator-interaction read. | O/P paired-delta evidence + RF2 verdict; one Stage-1 decision **slipped ~06-17 → ~late-July** (O/P arms launched 06-11, 2 days of data as of 06-13) |
| 4 | Chain-rebuild stale-input artifacts | Investigate-and-reorder OR auto-chain-rebuild | **Partially closed 2026-06-03**: `rebuilt_each_refresh` classification suppresses transient stale alerts (daily review runs at step 14, before steps 17-38 rebuild). Remaining: promotion-gated artifact mismatches (stage3_v2 `phase4_models.json` hash; calibrator_over vs calibrator_under divergence on the shared training table) — these are real and surface correctly. | Operator promotes / rebuilds the gated artifacts |
| 5 | Gate-counterfactual cross-window validation | **✅ Shipped 2026-05-26** | `window_reversal` flag + `lifetime_post_calibrator_enforce` 4th window; confidence auto-downgrades to `review_required` on reversal. First run flagged 4 of 9 HIGH-confidence recs as reversed. Closed. |
| 6 | Fleet-root integration gap | Loader change in `unified_signal_table` | **✅ DECIDED + shipped 2026-06-14: guarded fold-in.** `build_unified_signal_table.py --include-fleet` now materializes each `data/paper_<label>/*` root in the canonical schema (`config_label` per arm) into a **separate** `unified_signals_fleet/` artifact — wired into the daily refresh. The canonical `signals_master` (which feeds calibration / walk-forward / loss-attribution) is **byte-identical / never pooled**, avoiding the ~13-correlated-copies pseudo-replication. Honest caveat: with the guard, the fleet's unique signal (delta decisions) is already harvested by paired-delta + shadow-CLV `by_config_label`; the fold-in is an *affordance* for deliberate per-arm canonical analysis, not an automatic learning multiplier. | — (closed) |
| 7 | Daily review hard-fails without a live-root session file | **✅ Shipped 2026-06-11** | Missing session now degrades instead of raising: session-dependent blocks publish empties, `session_missing: true` stamped on the report, `Session-missing:` warning leads the Notes feed, and the fleet/B4/artifact blocks publish regardless. 2 new tests. Closed. |
| 8 | UNDER per-line calibration | Re-run `--per-line-min-rows` for `--side under` once data supports it | **Deferred 2026-06-11 with evidence**: per-line UNDER is overfit on current counterfactual-label data (held-out logloss 0.813 vs 0.711 pooled; line-5.5 isotonic maps raw 0.05→0.82). Revisit when real UNDER paper outcomes accumulate (B4 runway). | B4 sample growth |
| 9 | Calibrator-enforce floor `enforce_min_raw` 0.90→0.95 | One-line config (`signal_config.py` or override file) | **Recommended by the 06-14 audit, held for evidence.** Triple-corroborated: edge_shaving deep-dive verdict JUSTIFIED@0.95 ([0.95,1.0) = −17% ROI correctly blocked, [0.90,0.95) ~breakeven so shouldn't be shrunk); `L_enforce_min_raw_095` +$3.83 (COLLECTING); the daily "muting winners" alert (≈−$94/wk would-block, 7/7 days). Caveat: the day-level would-block cohort is noisier than the 30d aggregate (06-13 showed both bands net-negative — see the new band-split diagnostic). | More `L` evidence (firms on live re-entry) + sign-off |
| 10 | Gate `gate_max_base_fv` 0.99→0.95 | `promote.py gate-threshold` | **Recommended by the 06-14 audit, held for sign-off.** Cert's own sweep + gate_counterfactual + post-calibrator window all agree (+$73/30d, +$152 lifetime, no window-reversal; newly-blocked 0.95–0.99 band = 75 filled bets @ −15.8%). The cert's nominal `KEEP` verdict is a heuristic artifact — it only inspects the current threshold (3 blocked) and ignores its own sweep. The `Q_max_base_fv_095` fleet arm (2026-06-15) now accrues FV-level paired-delta evidence on it. | Live re-entry (filled-bet evidence) + sign-off |
| 12 | Entry-timing / liquidity-aware execution (the response to the CHASING finding) | Liquidity/quiet-book entry filter | **🔼 NEW 2026-06-15, now the top execution lever.** The shadow-CLV tape layer found the residual is **CHASING**: 97.7% of placed bets enter on a FLAT tape (no trades in 30s) into thin/wide books, and the 2-min adverse drift is quote-only. So the fix is cheap **execution-side**, not a model: don't bet into thin/quiet/wide books (liquidity floor, recent-trade-activity filter), wait for quote confirmation, and lean on the 06-04 `--max-limit-gap-below-ask` fill-gap cap. Scope the filter from the tape features (`trades_last_30s_count`, spread, `seconds_since_last_trade`). | Design the filter + live re-entry to measure |
| 11 | Market-anchored-alpha runtime shadow (unblocks T8 fleet arm) | 7-file live-pipeline ship (shadow only) | **🟦 QUEUED but ⬇️ DE-PRIORITIZED 2026-06-15.** Spec: [docs/operational/market-anchored-alpha-runtime-shadow.md](docs/operational/market-anchored-alpha-runtime-shadow.md). **Premise weakened:** the tape layer showed the adverse selection is **CHASING, not informed flow** (0/108 adverse losses had real selling against us), so a market-anchored "respond to informed flow" model is the WRONG lever — see Hygiene #12. What survives is only the narrow OOS-positive `no_score_drift`+`mid_no_vig` *calibration* improvement; keep this as that modest lever, below #12. | Operator sign-off; de-prioritized below Hygiene #12 |

### Research findings

| # | Finding | Status | Most-actionable follow-up | Read |
|---|---|---|---|---|
| RF1 | Edge Atlas — market structurally overprices Over by **+2-5pp across every cohort** (lines 9.5/10.5/11.5 + inn_8-9 worst), validating bidirectional pivot premise | ✅ Shipped 2026-05-27 | RF1.a (recent-N comparison) tests whether 10y baseline is stale before any live use of the finding | `data/analysis_output/edge_atlas/edge_atlas.md` |
| RF1.a | Recent-N comparison: bias **survives directionally** across 3y/4y/5y/6y/10y windows (0 sign flips / 16 buckets), magnitude varies. Verdict: `BIAS_PARTIALLY_SURVIVES`. Bidirectional pivot premise confirmed regime-stable. | ✅ Shipped 2026-05-27 | RF1.b realized-outcomes deep dive (awaits more N), RF1.c per-park stratification | `data/analysis_output/edge_atlas/recent_n_comparison.md` |
| RF2 | Fleet 2×2 redundant-correction interaction — Alt-A scope-enforce **helps** with the calibrator OFF (D vs C: +$55 delta) but **hurts** with it ON (B vs A: +$79 against scope, 9/15 days): both levers shrink overconfident FV, and doing both double-shrinks past winners. Tracked automatically by the fleet paired-delta block; B_cal_only at t=1.09, ~45 days from CONCLUSIVE as of 2026-06-13 (paper-week volume drop slowed it from the ~06-17 estimate) | 🔶 Pending verdict | Hold ALL Alt-A scope expansion (Active #8) until the verdict lands; if CONCLUSIVE_POSITIVE for B, the decision is scope-enforce OFF or calibrator-band rebalance, not more Alt-A | Daily review → `fleet_paired_delta_health` |

Last dashboard refresh: **2026-06-11**. Refresh after each ship.

---

Last reviewed: **2026-06-15** (paper-window optimization sprint + cert fix +
tape layer). Shipped the T1 shadow-CLV collector, then a **tape / real-trade
layer** that flipped the strategic read: the residual is **CHASING, not
informed flow** (97.7% of bets enter on a flat tape; 0/108 adverse losses had
selling against us) → market-anchored model (Hygiene #11) **de-prioritized**,
new **Hygiene #12 entry-timing/liquidity execution** is the top lever. Also:
guarded fleet fold-in (Hygiene #6 closed), B4 dormant, and fixed the cert
verdict blind spot + window-reversal guard (2 durable tighten RETUNEs:
`gate_max_base_fv`→0.95, `gate_min_inning`→6; reversals demoted). Full detail
in **[ROADMAP_CHANGELOG.md](ROADMAP_CHANGELOG.md#review-log)**.

## Recently completed

- **Stage-1 out-of-sample cache replay** *(2026-06-11, latest)* — the
  "shadow-override report" treatment for the NB cache, same day as its
  build: new `scripts/analysis/build_stage1_nb_replay_report.py`
  replays the NB + Alt-A staging caches against every settled 2026
  row via logit delta substitution (the variant cache's smoothing
  delta applied on top of production's runtime base, preserving
  fallback machinery; p3 = raw chain, the loss-attribution quantity).
  2026 rows are true out-of-sample — neither cache trains past 2025.

  **Findings (1,327 settled candidates + 116 placed bets)**:
  | Variant | Raw bias | Brier | Logloss |
  |---|---|---|---|
  | Production | +25.05pp | 0.2870 | 1.0700 |
  | NB tail | **+16.71pp** | 0.2573 | 0.8100 |
  | Alt-A (full) | **+15.56pp** | 0.2565 | 0.8124 |

  1. **Real but partial transfer**: NB's 93.4% in-corpus closure
     becomes ~33% out-of-sample. Both proper scoring rules improve
     materially, so the fix is genuine — but the residual ~16pp is
     **selection-driven** (the bot bets where the model is most
     overconfident; both caches agree on cells that look good
     historically and win less in market-selected situations).
     Better smoothing cannot close it; a market/selection-aware lever
     can (market-anchored alpha research, per-line stake controls).
  2. **NB ≈ Alt-A out-of-sample** — within 1.2pp bias / 0.001 brier
     on identical rows. Direct evidence they correct the SAME
     phenomenon (consistent with RF2's redundancy finding). NB does
     it with ~960 parameters, full coverage, and no per-cell
     empirical noise import; Alt-A needs scope rules. The Stage-1
     decision (slipped ~06-17 → ~late-July per the 06-14 audit; O/P
     arms only 2 days in) is now a like-for-like table; the O/P fleet
     arms add the live-fire + calibrator-interaction dimension.
  3. Worst cohort either way: line ≤6.5 (+38.6pp → +32pp) — the
     low-line slice stays broken under any smoothing; per-line
     calibrator + selection controls own it.
  4. Anchor sanity: mean |stored runtime base − current cache po| =
     0.020 on 616 exact rows — delta-substitution anchors are sound.

  Output: `data/analysis_output/stage1_nb_replay/
  stage1_nb_replay_report.{json,md}`. 6 new tests; full suite 1,567
  green.

- **Hygiene #3: negative-binomial Stage-1 tail — staging cache + two
  model-version fleet arms (+ Hygiene #7 guard)** *(2026-06-11)* —
  the first work attacking the chronic +18pp raw-FV bias
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

Earlier ships are in **[ROADMAP_CHANGELOG.md](ROADMAP_CHANGELOG.md)**;
shipped work from 2026-05-27 and earlier is in
**[ROADMAP_ARCHIVE_2026_H1.md](ROADMAP_ARCHIVE_2026_H1.md)**.

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
     verdict (**~late-July at current pace — slipped from the ~06-17
     estimate; the paper-week volume drop pushed B_cal_only from t=1.71/
     ~6d on 06-11 to t=1.09/~45d on 06-13, and the O/P Stage-1 arms have
     only 2 days of data**) — if CONCLUSIVE_POSITIVE, the right move
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

    **✅ DECIDED 2026-06-14 — guarded fold-in (the safe synthesis of
    (a)+(b)).** `build_unified_signal_table.py --include-fleet` reuses
    the tested loaders to materialize each `data/paper_<label>/*` root
    in the canonical schema (`config_label` per arm) into a **separate**
    `data/analysis_output/unified_signals_fleet/` artifact, wired into
    the daily refresh. The canonical `signals_master` — the input to
    calibration / walk-forward / loss-attribution — is **byte-identical
    and never sees fleet rows**, so the pseudo-replication risk that
    made (b) the lean is avoided by construction, while the fleet data
    is now foldable + filterable by `config_label` for deliberate
    per-arm analysis. (`config_label` was already a schema column,
    default `"default"`; the change only relabels fleet rows in the
    separate file.) Honest scope note: the fleet's unique signal — its
    *delta* decisions — is already captured by the paired-delta block
    and the new shadow-CLV `by_config_label`; this fold-in is an
    affordance, not the "learning multiplier" the gap was framed as
    (with the necessary guard, that multiplier is mostly illusory).
    The calibration-training loader was intentionally left unchanged
    (same quarantine principle). Test:
    `tests/test_fleet_signal_table_foldin.py`.

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

9. **Calibrator-enforce floor `enforce_min_raw` 0.90 → 0.95
    (recommended 2026-06-14, held for evidence).** The week-log audit
    found calibrator-enforce blocked net-winning would-bets all 7 days
    (06-07→06-13), ≈−$94 cumulative would-block counterfactual. The
    `calibration_edge_shaving` deep dive stratifies the enforce zone:
    [0.95,1.0) realizes −17.3% taker ROI (n=1020 — correctly blocked),
    [0.90,0.95) is ~breakeven (−1.0%, the band enforce shouldn't
    shrink), and [0.80,0.90) is +16% (already exempt). Verdict
    JUSTIFIED, recommended floor **0.95**. Corroborated live by
    `L_enforce_min_raw_095` (+$3.83, COLLECTING, t=0.63). **Held**
    because (a) the change is live-trading-affecting (takes effect on
    re-entry) and (b) the day-level would-block cohort is noisier than
    the 30d edge_shaving aggregate — on 06-13 both bands were
    net-negative to block, so the new band-split diagnostic in the
    daily review only recommends the floor raise when the
    [0.90,0.95)-muted / [0.95,1.0)-toxic signature actually holds.
    Promote when `L` firms up post-re-entry. Files: `signal_config.py`
    (`DEFAULT_PROB_CALIBRATION_ENFORCE_MIN_RAW`),
    `cache/live_engine_overrides.json`.

10. **Gate `gate_max_base_fv` 0.99 → 0.95 (recommended 2026-06-14,
    held for sign-off).** The gate_counterfactual recommends tightening
    (+$72.99/30d high-confidence, +$152 lifetime, +$93 post-calibrator,
    `window_reversal=false` — survives the Hygiene #5 cross-window
    guard). The walk-forward cert's own **sweep** agrees: at 0.95 kept
    ROI lifts +4.85% → +11.6% and the newly-blocked 0.95–0.99 band is
    75 filled bets at −15.8% ROI. The cert's nominal `KEEP` verdict is
    a **verdict-heuristic artifact** — it evaluates evidence only at the
    *current* threshold (3 filled bets blocked) and never consults its
    own sweep. **Held** because it changes live trading and overriding a
    `KEEP` verdict warrants explicit sign-off. Ship via
    `promote.py gate-threshold gate_max_base_fv 0.95` on live re-entry.
    **✅ De-risked 2026-06-15:** the cert per-gate verdict blind spot is
    fixed (it now runs a sweep-aware tighten check before the
    current-blocked-N early-return), so **the cert itself now returns
    `RETUNE → 0.95` (medium confidence)** for this gate — there is no
    longer a `KEEP` to override, removing the original reason this was
    held beyond live-fill evidence + sign-off.

11. **Market-anchored-alpha runtime shadow — QUEUED 2026-06-15 (unblocks
    the T8 fleet arm).** Full ready-to-execute ship spec:
    [docs/operational/market-anchored-alpha-runtime-shadow.md](docs/operational/market-anchored-alpha-runtime-shadow.md).
    *Motivation:* the T1 shadow-CLV collector found the selection-driven
    residual is **ADVERSE_SELECTION** (≈62% of losses drift away from us
    within 2 min — the market re-prices faster than we do). A market-side
    residual is closable by a market-anchored model, and the
    `calibration_market_anchored_alpha` walk-forward already has one
    OOS-positive arm: **`no_score_drift` anchored to `mid_no_vig`**
    (val-selected ROI +0.38, CI [0.47, 20.37] excludes 0). *Scope:* wire
    THAT arm only into the runtime in **shadow** (compute + log
    `fair_value_market_anchored`, no decision change), mirroring the
    Stage-1 Alt-A shadow pattern across ~7 live-pipeline files + one new
    model-apply helper + a `_market_anchored_alpha_shadow_health`
    daily-review surface. **Exclude `score_event_transition`** — it is
    OOS-negative (family gating is a correctness requirement). *Unblock:*
    once `--market-anchored-alpha-mode` exists, add fleet arm
    `R_market_anchored_nsd` (A_current + the flag) so paired-delta accrues
    live-fire evidence — the deferred T8. *Why queued not shipped:* it
    touches the live signal pipeline and the evidence is one
    marginally-significant walk-forward window, so it needs operator
    sign-off and is best sequenced on live re-entry (shadow evidence on
    real fills, not paper). Effort M–L. Files: see the spec doc.
    **⬇️ De-prioritized 2026-06-15** below Hygiene #12 — the tape layer
    (below) refuted its "informed flow" premise.

12. **Entry-timing / liquidity-aware execution — NEW 2026-06-15, the
    response to the CHASING finding.** The shadow-CLV **tape layer**
    (`build_shadow_clv.py`, joins each placed candidate to its real-trade
    `tape_captures` by `(config_label, bet_id)`) disambiguated the
    `ADVERSE_SELECTION` verdict: it is **CHASING, not informed flow**.
    Evidence — **97.7% of placed bets entered on a FLAT tape** (zero trades
    in the prior 30s), and of 108 adverse-drift losses **0 had real net
    selling against us** (`tape_subverdict = CHASING`,
    `_shadow_clv_health` surfaces it). So when we bet and the market drifts
    against us in 2 min, it is **quote movement in a thin/illiquid book**,
    not the market knowing something — we are entering quiet, wide books and
    the quote drifts off us (consistent with the −2.1c mean shadow-CLV).
    **The fix is execution-side and cheap**, not a model: a liquidity /
    quiet-book entry filter (skip when `trades_last_30s_count == 0` /
    `seconds_since_last_trade` is large / spread is wide), wait for quote
    confirmation, and the 2026-06-04 `--max-limit-gap-below-ask` fill-gap cap
    is the first related slice. Design the filter thresholds from the tape
    feature distributions; measure on live re-entry. This **outranks the
    market-anchored model (Hygiene #11)** — that lever answers a problem
    (informed flow) we don't have. Files: `build_shadow_clv.py` (diagnostic,
    shipped), `scripts/trading/live_pricing.py` / `signal_pipeline*.py`
    (the eventual filter, sign-off + re-entry gated).

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

## Operational guidance (per session)

These are standing rules, not roadmap items -- they apply every day
regardless of which Active priority is currently being worked.

- **Whenever live trading is paused, start the dry-run continuity engine
  the SAME day** (`live_engine.py --dry-run --no-startup-refresh`). The
  canonical learning loop (concept-drift PSI, calibration training table,
  session-dependent daily-review blocks) reads only the live root and goes
  blind without a session for the day. The 06-07→06-10 gap (continuity
  engine started 4 days late) left concept-drift PSI on 8 rows. Runbook +
  verification steps: [docs/operational/live-pause-continuity.md](docs/operational/live-pause-continuity.md).
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
