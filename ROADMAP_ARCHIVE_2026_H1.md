# Roadmap archive — 2026 H1 (Recently Completed, 2026-05-17 and earlier)

This file holds historical "Recently Completed" entries that were carved out
of the main [ROADMAP.md](ROADMAP.md) on 2026-05-25 to keep the active document
scannable. Nothing here is open work; treat it as a write-once, read-rarely
log of shipped features and bug fixes.

If you need to know what's *currently* in flight (Active Priorities, Hygiene,
or the bidirectional pivot), read ROADMAP.md, not this file. If you need to
know what shipped *since* this archive was carved (2026-05-18 onward), that
also lives in ROADMAP.md under "Recently completed."

Newest entries first within this archive; ordering matches the original
ROADMAP.md "Recently completed" section as of 2026-05-25.

---


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
