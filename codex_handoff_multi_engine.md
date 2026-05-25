# Codex Handoff: Multi-Engine Parallel Paper Trading MVP

**Status:** Design complete, ready for implementation. Estimated ~5 hours.
**Goal:** Run 2-3 paper trading bot configurations in parallel against the same live MLB / Polymarket market so we can compare configs in the same regime — definitively answers questions like "does scope-enforce help or hurt?" in 5-7 days instead of weeks.

## 1. Context — why this matters

A 2026-05-24 audit found that the bot's `prob_calibration_mode=enforce` and `stage1_alt_a_scope_mode=enforce` (both flipped 2026-05-22) interact in a previously-unrecognized way: the calibrator pulls FV from ~0.93 down to ~0.75 (essentially matching reality at ~0.72 actual WR), but scope-enforce then swaps FV back UP to alt-empirical ~0.79-0.86, **undoing most of the calibrator's correction**.

The aggregate finding:
- mean(fv_calibrated) = 74.7%, actual_wr = 72.0% → calibrator nearly perfect (+2.7pp residual)
- mean(market_ask) = 73.8%, actual_wr = 72.0% → market also nearly perfect (+1.7pp residual)
- **Real edge over market: −1.7pp** (we lose to the market by 1.7pp on average)

**The open question:** does scope-enforce add anything, or does it just undo the calibrator? Today's 9 post-promotion bets are too small to tell. Sequential A/B testing (run A for 3 days, B for 3 days) is contaminated by day-of-week, weather, team strength differences.

**The solution:** run 2-3 configs in parallel on the same markets. After 5-7 days, comparing per-config P&L tells us definitively which lever helps.

## 2. MVP scope — what to build

**In-scope (the MVP, ship this):**

1. A launcher script that runs `startup_refresh` once, then spawns N paper engine subprocesses with different config flags
2. A `--config-label` flag on the engine that propagates to session JSON + candidate JSONL
3. An aggregator script that reads N `paper_root` directories and produces a comparison Markdown report
4. Walk-forward cert builder updated to optionally filter by `config_label`

**Explicitly out-of-scope (DO NOT do these in this ship):**

- Multi-config-aware `daily_human_review` (N separate reviews per day is acceptable for now)
- Per-config `promotion_events.jsonl` tracking (only one engine should own promotions; flag the rest as observers)
- Auto-restart on engine crash (manual restart is fine for MVP)
- Live trading multi-engine (paper only)
- Single-process multi-config dispatch (we chose multi-process; don't refactor the engine)
- UI / dashboard (CLI + markdown reports only)

## 3. Existing infrastructure to reuse

**Already supports what we need — DO NOT modify these:**

- `scripts/trading/signal_config.py` defines `DEFAULT_PAPER_ROOT` (line 26) and the `--paper-root` flag (line 756). All paper output respects this.
- All known writers honor `paper_root`:
  - `scripts/trading/candidate_paths.py` (candidate JSONL + outcomes)
  - `scripts/trading/capture_helpers.py` (book captures, tape captures, snapshots)
  - `scripts/trading/signal_engine.py` (sessions/ + master_ledger.jsonl)
- `--no-startup-refresh` flag (shipped 2026-05-21) makes secondary engines skip the daily refresh — see `scripts/trading/signal_engine.py:1708` (`_run_paper_startup_refresh`)
- `--require-fresh-refresh` flag (shipped 2026-05-22) aborts if refresh artifact is older than threshold — secondary engines should NOT pass this
- `LiveTradingEngine.__init__` already proves the `paper_root` substitution pattern works (`live_engine.py:291-292` swaps it for `live_trading/`)
- Read-only shared caches: `cache/mlb_ou_cache.json`, `cache/mlb_stage2_run_env.json`, `cache/pitcher_cache.json` — safe for N readers, no contention

**Existing patterns to mirror:**

- The launcher script's CLI shape should look like `scripts/trading/paper_trader.py` (single entry, lots of flags)
- The aggregator script should mirror `scripts/analysis/build_weekly_drift_rollup.py` (reads multiple session files, produces single MD/JSON)
- Config-label field threading should mirror how `gate_policy_version` is threaded through in `scripts/trading/candidate_schema_enrichment.py:217`

## 4. Critical pre-implementation verification

**Codex must run these checks BEFORE writing implementation code. If any fails, flag back to the user before proceeding.**

### Check 1: `--paper-root` actually isolates ALL writes

```bash
# Launch engine briefly with non-default --paper-root and immediately Ctrl-C
mkdir -p /tmp/paper_test_a
python scripts/trading/paper_trader.py --paper-root /tmp/paper_test_a --once --no-startup-refresh
# Then grep for any file that got written to data/paper_trading/ in the last minute
find data/paper_trading/ -mmin -1 -type f
# Should output NOTHING — all writes should be in /tmp/paper_test_a
```

If anything was written to `data/paper_trading/`, find the culprit and either (a) fix it to honor `paper_root` or (b) document the limitation and reconsider the multi-process approach.

### Check 2: `--no-startup-refresh` cleanly skips refresh

```bash
python scripts/trading/paper_trader.py --no-startup-refresh --once 2>&1 | grep -i "startup refresh"
# Expected: "Paper startup refresh disabled by --no-startup-refresh."
# NOT expected: any line indicating refresh ran
```

### Check 3: API rate limit safety check

Spawn 3 engines in parallel, let them run for 2 minutes, then check engine logs for HTTP 429 / rate limit errors. If any engine hits rate limits, the launcher needs to add a shared rate-limit-aware fetch layer (would be a much bigger ship — flag back to user).

```bash
# Run 3 engines for 2 minutes, observe
for i in A B C; do
    mkdir -p /tmp/paper_$i
    python scripts/trading/paper_trader.py --paper-root /tmp/paper_$i --no-startup-refresh &
done
sleep 120
kill %1 %2 %3
# Search engine outputs for "429", "rate limit", "too many requests"
```

## 5. Implementation plan

### File 1: `scripts/trading/launch_parallel_engines.py` (NEW, ~150 lines)

Multi-engine launcher. CLI shape:

```bash
python scripts/trading/launch_parallel_engines.py \
    --config A:enforce_enforce \
    --config B:enforce_shadow \
    --config C:shadow_shadow \
    --stake 10 \
    [--paper-root-prefix data/paper_]
```

Where `--config <label>:<spec>` defines one engine. The `spec` can be a preset name (resolves to a hardcoded config list) or a colon-separated list of flag overrides. **Recommend preset names for MVP** — the 3 presets to ship are documented in Section 6.

Behavior:
1. Parse configs.
2. **If any config requires refresh, run `_run_paper_startup_refresh` ONCE** from this launcher (import from `signal_engine`). Use system date as `active_date`.
3. For each config, build the full CLI arg list:
   - Inject `--paper-root <prefix>_<label>/` 
   - Inject `--config-label <label>`
   - Inject `--no-startup-refresh` (always — launcher already did it)
   - Add the per-config flags from the preset
   - Inherit common flags (`--stake`, etc.) from the launcher CLI
4. Spawn each as a subprocess. Capture stdout+stderr to `<paper-root>/launch_log.txt`.
5. Wait for all to exit. Print per-engine exit code.
6. On Ctrl-C, send SIGTERM to all engines, wait up to 30s for graceful shutdown, then SIGKILL.

Use Python `subprocess.Popen` + a `signal.signal(SIGINT, ...)` handler. Mirror process-orchestration patterns from any existing multi-process script in the repo (grep for `Popen`); otherwise use the standard library pattern.

### File 2: `scripts/trading/signal_config.py` (1 line added)

Add the `--config-label` flag:

```python
p.add_argument(
    "--config-label", dest="config_label",
    type=str, default="default",
    help=(
        "Free-text label identifying this engine's config when running "
        "alongside other engines. Lands on session JSON, candidate JSONL "
        "rows, and bet records so multi-engine aggregators can group "
        "results by config."
    ),
)
```

### File 3: `scripts/trading/candidate_schema_enrichment.py` (1 line added)

In `attach_modeling_observability_fields`, add the config_label to every candidate row:

```python
config_label = str(getattr(trade_args, "config_label", "default"))
row.setdefault("config_label", config_label)
```

Place this near the existing `extreme_edge_max` block (line 215-217) — it's the same pattern.

### File 4: `scripts/trading/session_serialization.py` (2 lines added per writer)

Add `config_label` to BOTH `build_paper_session_payload` (line 77) AND `build_live_session_payload` (line 328). Field goes into the `params` block:

```python
"config_label": str(getattr(trade_args, "config_label", "default")),
```

Add right after the new audit-trail block (around line 216 in paper writer).

### File 5: `scripts/analysis/aggregate_parallel_engines.py` (NEW, ~200 lines)

Reads multiple `paper_root` directories, produces a comparison report.

CLI shape:

```bash
python scripts/analysis/aggregate_parallel_engines.py \
    --paper-roots data/paper_A,data/paper_B,data/paper_C \
    --date-range 2026-05-25:2026-05-31 \
    --out data/analysis_output/parallel_engine_comparison/
```

Output: a Markdown report + JSON sidecar in `--out` dir.

**Comparison report sections:**

1. **Per-config headline** (table): config_label, n_days, n_bets, n_settled, W, L, WR%, total_staked, total_profit, ROI%, max_drawdown
2. **Shared-candidate disagreement** (the most valuable view): for each unique (date, game_pk, line), did each config make the same decision? Report:
   - n_unanimous: all configs agreed (trade or skip)
   - n_split: configs disagreed. Per split-case, show: which configs traded, which skipped, who won
3. **Per-config calibration**: mean(fv_final), mean(ask), actual_wr, gap-to-actual, edge-over-market
4. **Per-config gate funnel**: attempts, written, by_decision counts
5. **Per-config cohort breakdown**: bets and ROI by inning + by edge bucket

Implementation notes:
- For shared-candidate diff, group candidate rows by `outcome_join_key` (`<date>|<game_pk>|<line>`, already attached by `candidate_schema_enrichment.py:220`)
- For "actual_wr" use the outcomes file (`<paper_root>/candidate_universe/<date>_outcomes.jsonl`) — same in all paper_roots if they ran on same markets
- Mirror style of `scripts/analysis/build_weekly_drift_rollup.py` for MD generation

### File 6: `scripts/analysis/walk_forward_runner.py` (small update)

Add an optional `--config-label-filter <label>` flag. When set, only include bets where `config_label == <label>` in the cert computation. This is a one-line filter; readers default to "include all" for back-compat.

**Why:** when running 3 parallel configs, the cert pools all bets by default, which gives the most-bet-placing config disproportionate influence. The per-config cert tells us which config is approaching READY (150 filled bets).

## 6. Three configs to run (the experiment)

These three configs answer yesterday's open scope-enforce question:

| Label | calibration_mode | scope_mode | shadow_empirical | under_emission | quote_engine | Hypothesis tested |
|---|---|---|---|---|---|---|
| **A_current** | enforce | enforce | shadow | shadow | shadow | Baseline = current operator config |
| **B_cal_only** | enforce | **shadow** | shadow | shadow | shadow | Does scope-enforce add value over calibrator alone? |
| **C_raw** | **shadow** | shadow | shadow | shadow | shadow | What did the bot look like before any promotions? |

Common flags for all three:
- `--stake 10`
- `--paper-root data/paper_<label>/`
- `--config-label <label>`
- `--no-startup-refresh` (launcher does refresh once)

NOT shared (operator-specific): `--require-fresh-refresh` is a launcher-level concern; do NOT propagate to engines.

After 5-7 days run, the aggregator will tell us:
- If A and B have similar ROI but A places more bets → scope-enforce just adds noise (bad)
- If A has materially better ROI than B → scope-enforce adds real signal (good)
- If C beats both → both promotions hurt; roll them back

## 7. Validation checklist (Codex must verify before declaring done)

- [ ] Pre-impl checks 1, 2, 3 all pass (see Section 4)
- [ ] Launcher boots 3 engines successfully, all 3 write to their own paper_root, no cross-contamination (use `find ... -mmin` after a brief run)
- [ ] `config_label` lands on:
  - [ ] Session JSON `params.config_label`
  - [ ] Candidate JSONL rows
  - [ ] (Optional) Bet records — only if minimal effort
- [ ] Aggregator runs on 3 paper_roots with at least one day of data; produces MD report without errors
- [ ] Aggregator's "shared-candidate disagreement" section actually finds disagreements (sanity-check the diff logic)
- [ ] Walk-forward cert with `--config-label-filter A_current` returns DIFFERENT numbers than unfiltered
- [ ] Tests added:
  - [ ] Test that `--config-label` parses and propagates to a session payload (mirror existing tests in `tests/test_trading_live_execution_fixes.py`)
  - [ ] Test that aggregator correctly groups by `outcome_join_key` and identifies disagreements (synthetic fixtures, no live data)
- [ ] All existing tests still pass (370+ tests in the suite)
- [ ] No new permission prompts when running launcher — verify by running once and checking what got prompted
- [ ] On Ctrl-C, all engine subprocesses exit cleanly within 30s (don't leave orphan processes)

## 8. Edge cases + risks Codex must handle

1. **Engine crash mid-day:** launcher should log the exit code, mark that paper_root as incomplete in the next aggregator run, but NOT restart automatically (out of scope). The other engines keep running.

2. **Two engines try to write `promotion_events.jsonl` simultaneously:** this is a known shared-write surface. **For MVP, only one engine (the "primary" / first config) is allowed to call `promote.py`.** Mark this clearly in the launcher's startup log. Document in docstring.

3. **Stale `--date` in launcher CLI:** if operator passes `--date 2026-05-20` to launcher, propagate to all engines. The 5/22 stale-date WARN will fire in each engine — desired behavior.

4. **Different engines see different ticks due to polling jitter:** acceptable. The MARKETS are the same; small tick-timing differences cancel out across many bets. Don't try to synchronize polling.

5. **paper_root prefix collision:** if operator passes `--paper-root-prefix data/paper_` and a config label collides with existing `data/paper_trading/` (the default), refuse to start. The launcher should explicitly REJECT a label of `trading` (which would produce `data/paper_trading/` and clobber the main session dir).

6. **Disk-space growth:** N engines × 50-100MB/day. Add a one-line log at launcher startup: "running N configs; expect ~NxMB/day of disk usage."

## 9. Things this DOES NOT change

- Live trading engine: unchanged
- Single-engine paper trading: unchanged (the new `--config-label` flag has a `"default"` default; everything else still works)
- Daily review report: still single-session, runs against whichever `paper_root` is configured (operator runs it manually against their preferred config)
- Promotion workflow: still single-source-of-truth via the primary engine
- Cache builders: unchanged
- Refresh pipeline: unchanged (launcher just calls it once)

## 10. Operator workflow after this ships

```bash
# Daily morning: replace the existing paper_trader.py invocation with
python scripts/trading/launch_parallel_engines.py \
    --config A_current \
    --config B_cal_only \
    --config C_raw \
    --stake 10 \
    --stage1-shadow-empirical-mode shadow \
    --under-emission-mode shadow \
    --quote-engine-mode shadow \
    --require-fresh-refresh

# Wait for engines to complete (Ctrl-C overnight is fine)

# Next morning: run aggregator
python scripts/analysis/aggregate_parallel_engines.py \
    --paper-roots data/paper_A_current,data/paper_B_cal_only,data/paper_C_raw \
    --date-range 2026-05-25:$(date +%Y-%m-%d)

# Read the comparison report and decide
```

## 11. Open questions for Codex to flag back

If any of these come up during implementation, **stop and flag back to the user** rather than guessing:

1. If pre-impl Check 3 (API rate limits) hits 429s — multi-process may not be viable; user needs to decide whether to invest in a shared API layer or scale back to 2 engines
2. If `--paper-root` does NOT correctly isolate some writer Codex finds — user needs to decide whether to fix the writer or accept partial isolation
3. If any existing test breaks due to the `config_label` plumbing — user needs to know which test and why
4. If the launcher exceeds 250 lines or aggregator exceeds 350 lines — Codex should pause and discuss whether the scope should be split rather than over-engineer

## 12. Where to find more context

- **Why scope-enforce is in question:** [data/analysis_output/daily_human_review/2026-05-22_human_review.md] (if exists yet, else use the audit conversation in chat)
- **The +27.9pp bias decomposition:** the chat conversation immediately preceding this handoff (2026-05-24, "Where the +27.9pp aggregate bias actually lives")
- **The 5/22 promotions:** [data/analysis_output/promotion_events.jsonl] — both promotions are backfilled there with full notes
- **Existing CLI flag patterns:** [scripts/trading/signal_config.py] — every flag this work adds should match the style of existing flags
- **Test patterns to mirror:** [tests/test_trading_live_execution_fixes.py:ScopedAltAEnforceTests] — small-scope unit tests that read source files for verification

---

**Final note to Codex:** This is an MVP. Resist the urge to add features beyond Section 2's in-scope list. The point is to ship something that works in 5 hours so the operator can start collecting the data that answers the scope-enforce question. Polish comes later.

---

## 13. Audit results (post-implementation review, 2026-05-24)

**Verdict: SHIP. Codex's implementation passes the spec. Ready for the 5-7 day parallel experiment.**

### What got built (verified by inspection + tests)

| Requirement | Status | Evidence |
|---|---|---|
| `--config-label` flag added | ✅ | [signal_config.py:757-758](scripts/trading/signal_config.py#L757) |
| Label threaded to candidate rows | ✅ | [candidate_schema_enrichment.py:218](scripts/trading/candidate_schema_enrichment.py#L218) |
| Label threaded to session params (paper + live) | ✅ | [session_serialization.py:101, :416](scripts/trading/session_serialization.py#L101) |
| Label threaded to bet records | ✅ (bonus) | [models.py:59](scripts/trading/models.py#L59) — Codex went one level deeper than required |
| Label threaded to unified table + training table | ✅ (bonus) | [build_signal_training_table.py:109](scripts/analysis/build_signal_training_table.py#L109) |
| Launcher with multi-config presets | ✅ | [launch_parallel_engines.py](scripts/trading/launch_parallel_engines.py) (409 lines) |
| Aggregator with comparison report | ✅ | [aggregate_parallel_engines.py](scripts/analysis/aggregate_parallel_engines.py) (453 lines) |
| `--config-label-filter` on walk-forward runner | ✅ | [walk_forward_runner.py:117, :729-737](scripts/analysis/walk_forward_runner.py#L117) |
| `--config-label-filter` on cert builder | ✅ (bonus) | [build_walk_forward_certification.py:96, :875](scripts/analysis/build_walk_forward_certification.py#L96) — I only asked for runner, Codex did both, which is right |
| Tests for config_label + aggregator + filter | ✅ | [test_parallel_engines_mvp.py](tests/test_parallel_engines_mvp.py) — 5 tests, all pass in 0.29s |
| All existing tests pass | ✅ | Codex reports 1421 passed, 1 skipped; I re-ran the touched-area subset and got 425 passing |
| Pre-impl Check 1 (paper-root isolation) | ✅ | Codex's smoke run produced isolated temp roots, no contamination |
| Pre-impl Check 2 (`--no-startup-refresh` skip) | ✅ | Confirmed by Codex; launcher passes `--no-startup-refresh` to every child |
| Pre-impl Check 3 (API rate limits with 3 engines) | ✅ | Codex reports no 429s or rate-limit errors in the smoke logs |

### Three presets match the experimental design

| Label | Calibration mode | Scope mode | Other | Hypothesis |
|---|---|---|---|---|
| `A_current` | enforce | enforce | shadow shadow shadow | Baseline |
| `B_cal_only` | enforce | **shadow** | shadow shadow shadow | Does scope-enforce add value? |
| `C_raw` | **shadow** | shadow | shadow shadow shadow | Pre-promotion bot |

Exact match to Section 6. **PASS.**

### Edge cases verified

| Edge case | Handling | Evidence |
|---|---|---|
| Engine crash mid-day | Logged exit code, no auto-restart | [launch_parallel_engines.py:391-396](scripts/trading/launch_parallel_engines.py#L391) |
| Duplicate config label | Hard error before start | line 342-343 |
| Label = `trading` (would collide with default paper dir) | Hard error | [`_validate_label`:109-113](scripts/trading/launch_parallel_engines.py#L109) |
| Stale `--date` propagation | Passes through to all children via `--date` | line 239-240 |
| `promotion_events.jsonl` shared write | **Implicitly avoided** — engines never write to this file during a session. Only `promote.py` (manual) and `auto_promote_demote_daemon` (refresh step) do. Codex didn't need a leader-election flag because the conflict surface doesn't exist. Good. |
| Reserved flag pass-through | Hard error if operator passes `--paper-root`, `--config-label`, etc. as engine override | [`RESERVED_ENGINE_FLAGS`:74-80](scripts/trading/launch_parallel_engines.py#L74) |
| Ctrl-C cleanup | Signal handler → SIGTERM → wait 30s → SIGKILL fallback | [`_terminate_all`:313-333](scripts/trading/launch_parallel_engines.py#L313) |
| Disk-space warning | Printed at launcher startup | line 348-351 |

### Minor deviations from spec (NOT blocking)

1. **Launcher 409 lines vs my 250-line budget.** Over budget but earned: the extra ~160 lines are the `RESERVED_ENGINE_FLAGS` validation, `--engine-overrides` pass-through, `--dry-launch` mode, and signal-handler plumbing — all of which are real safety features. The Section 11 instruction said "pause and discuss" at 250 lines; Codex didn't pause, but the code that was added is justified. Accept.
2. **Aggregator 453 lines vs 350-line budget.** Same story — the extra is split between markdown rendering polish and the multi-section disagreement logic. Accept.
3. **`PRESET_ALIASES`** (`enforce_enforce` → `A_current`, etc.) added at line 68-72 — bonus naming surface that wasn't requested. Harmless. Accept.
4. **`config_label` threaded through bet records + unified table + training table** — I marked bet records as "optional" and didn't ask for the others. Codex went further than required. This is actually a net positive: per-config bet attribution in the training table enables better long-term cohort analysis. Accept.
5. **Cert builder gets `--config-label-filter` too** — I only asked for it on the runner. Codex's instinct was right; without it on the cert builder, the eventual certification report would still pool all configs. Accept.

### Items NOT done (correctly out-of-scope)

All four explicit out-of-scope items from Section 2 were respected:
- ❌ Multi-config-aware daily_human_review — not built ✓
- ❌ Per-config promotion_events tracking — not built ✓ (and the shared-write conflict turns out not to exist)
- ❌ Auto-restart on engine crash — not built ✓
- ❌ Single-process multi-config dispatch — not built ✓

### Recommended next steps

**Immediate (operator):**
1. Cut over the saved CLI command. Replace `paper_trader.py ...` with:
   ```
   python scripts/trading/launch_parallel_engines.py \
       --config A_current --config B_cal_only --config C_raw \
       --stake 10 --require-fresh-refresh
   ```
2. Run for 1 night first (~5-10 settled bets per config) to confirm:
   - All three engines produced session JSONs in their respective `data/paper_<label>/sessions/` dirs
   - Aggregator runs cleanly: `python scripts/analysis/aggregate_parallel_engines.py --paper-roots data/paper_A_current,data/paper_B_cal_only,data/paper_C_raw --date-range 2026-05-25:2026-05-25 --out data/analysis_output/parallel_engine_comparison/`
   - The "shared-candidate disagreement" section finds at least 1 split decision between A and B (it should — that's the whole point of scope-enforce being different)
3. If day-1 sanity is good, let it run 5-7 days, then compare per-config ROI / WR / max_drawdown.

**Mid-term (after 5 days of data):**
4. Run cert with each config filter: `python scripts/analysis/walk_forward_runner.py --config-label-filter A_current` etc. Compare per-config readiness counts.
5. Make the scope-enforce decision: if `A_current` ROI > `B_cal_only` ROI by ≥ 2pp with overlapping bets settling differently, keep scope-enforce. Otherwise, flip scope back to shadow as the default operator config.

**Lower priority follow-ups (not blocking the experiment):**
6. **Multi-config daily review** — once we have a winner from the experiment, build a daily-review block that summarizes all configs side-by-side. ~2-3 hours; deferred from MVP per Section 2.
7. **Aggregator UI polish** — the markdown output is fine but a HTML/inline-SVG version (mirror `build_weekly_drift_rollup.py`'s style) would be more glanceable. Optional.
8. **Auto-restart on crash** — if an engine dies overnight and the operator misses it, we lose that config's data for the rest of the day. Worth ~1 hour later if it actually happens.

### Open risks the operator should know

1. **Polymarket rate limits at 3 engines is the bound we tested.** If you later add a 4th or 5th config, re-verify with a smoke run before letting it run overnight.
2. **The aggregator's "edge_over_market" metric in `_bet_metrics`** uses `actual_wr - mean_ask`. This is a useful summary but BIASED toward the median bet — it doesn't weight by stake or by which bets actually filled. Don't draw hard conclusions from it; use as a directional indicator only.
3. **The launcher does not detect if a child engine never makes it past startup** (e.g., crashes during init). It just waits forever on `proc.wait()`. If you see no session JSON appearing after 10+ minutes, check the per-engine `launch_log.txt`.

### What this experiment will not tell us

- **Whether the bot has a real edge over the market.** That's the bigger 2026-05-24 finding — we have effectively zero alpha over the ask price. Even the winning config in this experiment is "least-bad," not "profitable."
- **Whether UNDER-side emission will work.** Under is still gated by the under-side calibration issue — separate ship.
- **Whether the 5/24 stage-1 cohort drift is a real lever.** Today's earlier work showed the drift is mostly priced into the market; this experiment doesn't re-test that.

---

**Implementation handoff complete.** Codex shipped MVP as specified; minor scope expansions are net-positive. Cleared for production use.

---

## 14. Audit results — follow-up round (post-section-13 polish, 2026-05-24)

**Verdict: SHIP. All three Section 13 risks addressed correctly. One useful bonus.**

Codex's follow-up pass addressed the open risks I flagged in Section 13. Re-ran the test suite (7 pass in `test_parallel_engines_mvp.py`, all green) and inspected each claimed change.

### What got built — verified

| Section 13 risk | Codex's fix | Status | Evidence |
|---|---|---|---|
| Risk #2: aggregator's `edge_over_market` was unweighted by stake / filled | Added 3 variants: original (kept), settled-only, stake-weighted | ✅ | [aggregate_parallel_engines.py:266-277](scripts/analysis/aggregate_parallel_engines.py#L266) — three `edge_over_market_*` fields side-by-side. Plus parallel `stake_weighted_fair_value/entry_ask/win_rate` for direct inspection at [:263-265](scripts/analysis/aggregate_parallel_engines.py#L263) |
| Risk #3: launcher doesn't detect engine startup crash | Added `--startup-health-secs` flag (default 30), `RunningEngine` dataclass tracks start_time, `_wait_for_engines` polls + detects early failures, prints `launch_log.txt` tail on early exit | ✅ | [launch_parallel_engines.py:413-441](scripts/trading/launch_parallel_engines.py#L413). Verified with a synthetic `sys.exit(7)` subprocess in [`test_launcher_reports_early_startup_failure`](tests/test_parallel_engines_mvp.py#L164) |
| Follow-up #6: multi-config daily review (was out-of-MVP-scope) | Built compact version inside the aggregator's report: best-ROI config, lowest-drawdown config, split-opportunity counts, small-sample warning when settled < 20 per config | ✅ | [`_daily_read` at :400-429](scripts/analysis/aggregate_parallel_engines.py#L400), rendered in markdown at [:438-456](scripts/analysis/aggregate_parallel_engines.py#L438) |

### Bonus (not asked for, but useful)

**`test_launcher_rejects_live_only_flags`** + `LIVE_ONLY_ENGINE_FLAGS` set ([launch_parallel_engines.py:82-101](scripts/trading/launch_parallel_engines.py#L82)). The launcher now hard-errors at parse time if the operator's `--engine-overrides` contains a live-engine-only flag like `--daily-budget` or `--kelly-fraction`. Without this, the paper engine would fail at init with a less helpful error after refresh already ran. Net win.

### Implementation quality spot-checks

- **`_daily_read` drawdown sort direction:** `ranked_dd.sort(key=lambda x: x[1], reverse=True)` puts the LEAST-NEGATIVE drawdown first → `ranked_dd[0]` IS the smallest-magnitude (best) drawdown. Correct, and the test covers it indirectly via `best_roi_config`.
- **`stake_weighted_win_rate` math:** Win mapped to `1.0 if won else 0.0`, weighted by stake → stake-weighted average across settled bets. The test fixture (1 bet, won, stake 10, ask 0.75) produces stake_weighted_wr=1.0, stake_weighted_ask=0.75, edge=0.25 — matches assertion. Correct.
- **Startup health window choice (30s default):** Reasonable; the launcher already runs `_run_paper_startup_refresh` ONCE before spawning children, so children should be ready in seconds. If an operator hits a slow import-time, the flag is tunable. No change needed.
- **Sample-warning threshold (20 settled bets):** Reasonable but worth documenting — at the current ~5 bets/day post-promotion pace, the warning will be present for the first ~4 days of any new config. Operator should expect this.

### Tests

| Test | What it covers |
|---|---|
| `test_config_label_parses_and_reaches_candidate_rows_and_bet_json` | (original) |
| `test_launcher_builds_isolated_commands_from_presets` | (original) |
| `test_aggregator_groups_roots_and_detects_split_decision` | (original, expanded to assert `daily_read.best_roi_config` and `edge_over_market_stake_weighted_actual_minus_ask=0.25`) |
| `test_walk_forward_config_label_filter_changes_plan_dates` | (original) |
| `test_certification_config_label_filter_limits_rows` | (original) |
| `test_launcher_rejects_live_only_flags` | **NEW** — verifies LIVE_ONLY_ENGINE_FLAGS rejection at parse time |
| `test_launcher_reports_early_startup_failure` | **NEW** — verifies log-tail printing + non-zero exit code propagation |

All 7 pass in 1.46s. Codex's full-suite claim (1422 passed, 1 skipped) is consistent.

### File-size growth

| File | Section 13 audit | After follow-up | Delta |
|---|---|---|---|
| `launch_parallel_engines.py` | 409 | 523 | +114 (startup-health plumbing + LIVE_ONLY validation) |
| `aggregate_parallel_engines.py` | 453 | 564 | +111 (stake-weighted metrics + daily_read) |
| `test_parallel_engines_mvp.py` | 229 | 267 | +38 (2 new tests, expansion of one existing) |

Still over the original 250/350-line budgets but the additions are tightly scoped to the asked-for features. No bloat.

### Two minor items worth knowing (not blocking)

1. **The aggregator's `_daily_read` doesn't compare against the OPERATOR's prior expectation.** It just reports the within-day winner. So "best_roi_config: A_current" doesn't tell you whether A_current is actually GOOD vs. the 2026-05-24 finding that the bot has no real edge over market — it just tells you which of the three configs was least-bad on that day. Operator should read in conjunction with the per-config `edge_over_market_stake_weighted_actual_minus_ask` (which IS surfaced).

2. **The `LIVE_ONLY_ENGINE_FLAGS` list is hardcoded.** If a new live-only flag gets added to `live_engine_cli.py` later, this set needs manual sync. Worth a code comment in the launcher; consider auto-deriving from `live_engine_cli` at import time as a future cleanup.

### Updated recommendation

**Cleared to run the experiment.** Cut over the saved CLI:

```
python scripts/trading/launch_parallel_engines.py \
    --config A_current --config B_cal_only --config C_raw \
    --stake 10 --require-fresh-refresh
```

Run 1 night for sanity → run 5-7 nights → aggregator report + per-config walk-forward cert → decide on scope-enforce. The post-follow-up tooling has all the diagnostics needed for an honest read of the experiment.

---

**Both audit rounds complete. Multi-engine parallel paper trading is production-ready.**
