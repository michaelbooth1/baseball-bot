# OVER Gate Stack

Complete, source-verified reference for every gate the OVER (production)
trade pipeline applies, in execution order. The OVER side is the mature,
live-enforced strategy.

**Generated from code 2026-05-28.** Source of truth (do not let this doc
drift from them):
- `scripts/trading/signal_pipeline_gates_pre_fv.py` — early + pre-inference gates
- `scripts/trading/signal_pipeline_gates_post_fv.py` — FV-phase + post-FV gates
- `scripts/trading/signal_config.py` — all `DEFAULT_*` thresholds
- `scripts/trading/live_engine_placement.py` — placement-time caps (live only)
- Companion: **[UNDER_GATES.md](UNDER_GATES.md)**

A gate "fires" = the candidate is skipped and a skip row is written with the
listed `reason` string. Order matters: the first gate to fire stops the tick.

> **⛔ Disabled in production (2026-05-28, pending re-evaluation):**
> `gate_extreme_edge` (`--extreme-edge-max 1.0`) and
> `gate_stage2_suppression` (`--s2-suppress-max -99.0`). The OVER gate EV
> audit (`scripts/analysis/audit_over_gate_ev.py`) found S2-suppression
> −EV (threw away +10.6% taker ROI) and extreme-edge only marginal
> (~breakeven). Compiled defaults are intentionally left protective
> (`0.22` / `−0.20`) so the boot-time drift heartbeat stays armed and the
> paper fleet keeps the enforced-gate A/B. Re-enable = drop the two flags.

---

## Pipeline phases at a glance

```
tick → [1] early gates (environment)        evaluate_early_gates
     → [2] pre-inference gates (game state)  evaluate_pre_inference_gates
     → [3] FV inference + FV-phase gates     run_inference_and_fv_phase
     → [4] post-FV gates (model output)      evaluate_post_fv_gates
     → [5] placement-time caps (live only)   place_bet
     → ORDER PLACED
```

A candidate must clear **every** phase to become a bet.

---

## Phase 1 — Early gates (environmental) · `evaluate_early_gates`

"Do we even bother running the model?" These fire before any model work and
write *early-skip* rows (sampled for high volume).

| # | reason | Fires when | Default(s) | Why |
|---|---|---|---|---|
| 1 | `gate_min_inning` | line < 8.5: `inning < 4`; line ≥ 8.5: `inning < 5` | `min_inning=4`, `min_inning_high_line=5`, `high_line_cutoff=8.5` | Too many innings remain → too much variance. High lines need more settled state. |
| 2 | `gate_inactive_inning_state` | `inning_state ∈ {end, middle}` | `INACTIVE_INNING_STATES={"end","middle"}` | No active play during inning transitions; book is stale. |
| 3 | `gate_min_entry_ask` | line < 8.5: `ask < 0.55`; line ≥ 8.5: `ask < 0.60` | `min_entry_ask=0.55`, `min_entry_ask_high_line=0.60` | Thin / noisy books below the floor. |
| 4 | `gate_crossed_book` | `bid ≥ ask` | — | Crossed/locked book; unusable. |
| 4 | `gate_wide_spread` | `ask − bid > 0.20` | `max_spread=0.20` | Spread too wide to price/execute. |
| 5 | `gate_ask_jump_unconfirmed` | ask jump not confirmed: jump `< 0.06` over `5` ticks, or not sustained `3` ticks | `jump_threshold=0.06`, `lookback=5`, `confirmation_ticks=3` | The candidate generator: only act on a sustained ask move (proxy for a scoring-relevant event). |

Gates 1, 3 also log *shadow-relaxed* counters (would it pass at a −1 inning /
−0.05 ask floor) for tuning; these never change the decision.

---

## Phase 2 — Pre-inference gates (game state) · `evaluate_pre_inference_gates`

Game-state filters that block known-bad scoring contexts before FV inference.
Several support a **conditional relax** mode (off / shadow / enforce / A·B)
that can re-admit a narrow sub-cohort; default behavior is the strict gate.

| # | reason | Fires when | Default(s) | Why |
|---|---|---|---|---|
| 6 | `gate_min_current_total` | `current_total < 4` | `min_current_total=4` (relax floor 3 @ inn≥4, ask≥0.60, lead≤4, rn≤4.5) | Too little has happened to trust the over. |
| 7 | `gate_runs_pace` | `(current_total / inning) × 9 < line − 1.5` | 1.5-run buffer | Game is objectively too slow-scoring for the line. |
| 8 | `gate_runs_needed_max` | `runs_needed (= line − current_total) > 3.5` | `runs_needed_max=3.5` | Historically poor ROI at this distance. |
| 8b | `gate_close_game_runs_needed` | `lead < 2` AND `runs_needed ≥ 4.0` | `min_close_game_rn=4.0` | Close game → defense-first baseball suppresses scoring. |
| 8c | `gate_inning5_runs_needed` | `inning == 5` AND `runs_needed ≥ 2.5` | `inn5_rn_max=2.5` | Bullpen-transition inning suppresses scoring. |
| 8d | `gate_inning6_runs_needed` | `inning == 6` AND `runs_needed ≥ 2.5` | `inn6_rn_max=2.5` | Setup-reliever lull; 5yr backtest 63% WR on 1569 bets. |
| 8e | `gate_blowout` | `trailing_runs (= min score) ≤ 1` AND `inning ≥ 6` AND (`lead ≥ 6` OR (`lead ≥ 4` AND `inning ≥ 7`)) | `blowout_lead_min=6`, `blowout_adj_lead_min=4` (relax: inn≤7, ask≥0.74, rn≤2.5) | Poisson FV inflates 11–30pp when the trailing team is functionally shut out late. |

---

## Phase 3 — FV inference + FV-phase gates · `run_inference_and_fv_phase`

Runs the 3-stage fair-value model on the inferred post-event state. Gates here
fire on model internals.

| reason | Fires when | Default | Why |
|---|---|---|---|
| `inference_no_match` | Stage-1 cache returns None for all 3 inferred run counts (+1/+2/+3) | — | No supported cell to price from. |
| `gate_fv_saturation` (8f) | `base_fv` (Stage-1 Poisson) `≥ 0.99` | `max_base_fv=0.99` | Poisson ceiling = phantom API score update; only 1.0 is true saturation. |
| `gate_stage2_suppression` (8h) | `stage2_run_env_delta ≤ −0.20` AND `inning ≥ 6` | `s2_suppress_max=−0.20`, `s2_suppress_min_inning=6` | Extreme park/weather suppression exceeds Stage-2's correction capacity. **⛔ DISABLED in production 2026-05-28** via `--s2-suppress-max -99.0` — the OVER gate EV audit found it −EV (blocked overs hit 80.8% at ask 0.718 → +10.6% taker ROI thrown away). Re-evaluate later. |

**FV chain:** `base_fv` (Stage-1 Poisson/empirical) → +Stage-2 run-env logit
delta → +Stage-3 team-offense logit delta → **probability calibration** →
final `fair_value`. `edge = fair_value − ask`.

**Probability calibration (band-gated enforce, TR23):** not a gate, but it
reshapes FV → edge. `DEFAULT_PROB_CALIBRATION_MODE = "enforce"`; the Platt
calibrator overwrites raw FV **only when `raw ≥ 0.90`**
(`DEFAULT_PROB_CALIBRATION_ENFORCE_MIN_RAW = 0.90`); the mid-band keeps raw FV.
This materially shrinks high-FV edges and is the dominant reason production
bets rarely. See `docs/operational/fv-recalibration-2026-05-19.md` and
`scripts/analysis/analyze_calibration_edge_shaving.py`.

**Scoped Alt-A enforce (TR25):** when `--stage1-alt-a-scope-mode enforce`, the
cohort-aware empirical override may replace the Stage-1 Poisson FV (held to
Poisson for `inning ≥ 8`). Default `shadow`.

---

## Phase 4 — Post-FV gates (model output) · `evaluate_post_fv_gates`

"Does the model output justify a trade?" Evaluated **in this order**:

| # | reason | Fires when | Default(s) | Status |
|---|---|---|---|---|
| 1 | `gate_min_edge` | `edge < min_edge_effective` | base `edge_threshold=0.15` (`0.16` high-line) **+ ask-edge ramp boost** | Enforced |
| 2 | `gate_extreme_edge` (8f) | `edge > 0.22` in **any** inning | `extreme_edge_max=0.22` (TR17 enforce 2026-05-01; TR19 tightened 0.30→0.22) | **⛔ DISABLED in production 2026-05-28** via `--extreme-edge-max 1.0` — EV audit found the blocked cohort ~breakeven (+0.4%, n=139), not the clear winner TR17/TR19 claimed. Re-evaluate later. |
| 3 | `gate_line_high_fv_block` (8f.5) | `line ∈ {5.5}` AND `base_fv ≥ 0.90` | `line_high_fv_block_mode=off` | **OFF in prod** (paper `K_line5p5_block` A/B only) |
| 4 | `gate_fv_ask_gap` (8g) | `edge > 0.26` AND `inning ≥ 7` | `fv_ask_gap_max=0.26`, `fv_ask_gap_min_inning=7` | Enforced — late-inning market-disagreement / phantom fingerprint |
| 5 | `gate_sp_era` (8i) | `pitcher_era < 3.75` AND `inning ≤ 6` AND `edge < min_edge + 0.03` | `sp_era_threshold=3.75`, `sp_era_max_inning=6`, `sp_era_edge_boost=0.03` | Enforced when pitcher cache present (fallback ERA 4.20 disables it) |
| 6 | `gate_event_dedup` (9) | within `60s` of last bet on same game AND `edge ≤ last_edge` | `event_dedup_secs=60` | Enforced |
| 7 | `gate_inning_dedup` (10) | `< 2` innings since last bet on same line AND `edge_improvement ≤ 0.05` | `inning_dedup_gap=2`, `inning_dedup_edge_gap=0.05` | Enforced |

**Ask-edge ramp** (`min_edge_effective`): when `ask_edge_ramp_enabled` (default
True), `min_edge` is boosted up to `+0.05` linearly between `ask=0.75`
(start) and `ask=0.90` (end) — high asks demand more edge. So at a 0.89 ask the
effective min_edge is ~0.197, not 0.15.

---

## Phase 5 — Placement-time caps (live only) · `place_bet`

After all gates pass, live placement can still skip
(`engine._last_place_bet_skip_reason`):

| reason | Fires when | Default |
|---|---|---|
| `fresh_book_unavailable` / `fresh_book_invalid` | execution-time book missing or spread > max | `max_spread=0.20` |
| `limit_unplaceable` | can't post a resting limit while preserving min edge | `spread_factor=0.65` |
| `stake_below_min_order` | computed stake `< $5` | `min_order_size=5` |
| `ev_policy_block` | EV-policy enforce rejects (p_fill·EV) | `ev_policy_mode` default **shadow** → never blocks |
| `budget_exhausted` | exposure + stake > daily budget | `daily_budget` (prod 80) |
| `per_game_cap` | per-game exposure + stake > cap | `per_game_budget_fraction` (prod 0.40) |
| `correlated_line_count_cap` | ≥ 2 over-side bets already on this game | `max_correlated_over_lines_per_game=2` |
| `correlated_line_gap_cap` | new over line within 1.5 runs of an existing over line | `min_correlated_line_gap=1.5` |
| `max_open_orders` | open-order count at cap | `max_open_orders` (prod 7) |
| `wallet_cooldown` → `paper_fallback` | CLOB balance error / cooldown active | `wallet_exhausted_cooldown_secs=300` |

Correlated-line caps apply **per side** (2026-05-28) — OVER counts only OVER
exposure. Budget / per-game / max-open-order caps are **shared** across OVER
and UNDER.

---

## Sizing (not gates)

- **Limit price:** `bid + spread × 0.65`, capped at `fair_value − min_edge` and
  `ask − 0.01`, floored at `bid + 0.01`.
- **Stake:** flat (`--stake`) or quarter-Kelly on `daily_budget`
  (`kelly_fraction=0.25`, capped at `kelly_max_bet_fraction=0.33` of budget,
  edge capped at `kelly_max_edge=0.25`). Calibrated-edge stake scaling is
  shadow by default.

---

## Notes

- Gate numbering (1, 8e, 8f…) is historical (TR-series). The `reason` string is
  the stable identifier used in candidate rows and audits.
- Many gates carry conditional-relax or shadow-relaxed observability fields;
  those never change the live decision unless explicitly set to `enforce`.
- To print the live thresholds, read `signal_config.py` `DEFAULT_*` or the
  session JSON `params` block — do not trust hardcoded values here if they
  disagree with the code.
