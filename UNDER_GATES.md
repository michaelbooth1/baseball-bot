# UNDER Gate Stack

Complete, source-verified reference for every gate the UNDER trade pipeline
applies. UNDER trading went **live for real money on 2026-05-28**
(`--under-mode live`) as an operator-directed, **accepted-loss data-gathering
posture** — it is NOT a validated edge. Read the caveats at the bottom.

**Generated from code 2026-05-28.** Source of truth:
- `scripts/trading/signal_pipeline.py` — `_maybe_emit_under_candidate` (UNDER gates)
- `scripts/trading/signal_config.py` — `DEFAULT_UNDER_*` thresholds
- `scripts/trading/live_engine.py` — `_place_under_bet`, `_is_bet_executable`
- `scripts/trading/live_engine_placement.py` — `place_bet(side="under")`
- Companion: **[OVER_GATES.md](OVER_GATES.md)**

---

## CRITICAL precondition: UNDER inherits the OVER pre-FV stack

UNDER candidates are **not** evaluated independently from scratch. The UNDER
emitter (`_maybe_emit_under_candidate`) is called inside the OVER pipeline,
**after** `run_inference_and_fv_phase` succeeds but **before** OVER's post-FV
gates:

```
OVER early gates (1–5)  ─┐
OVER pre-inference (6–8e) ├─ must ALL pass for the tick to reach UNDER
OVER FV-phase (inference, 8f saturation, 8h S2-suppress) ─┘
        ↓
   _maybe_emit_under_candidate   ← UNDER's own gates run here
        ↓
OVER post-FV gates (min_edge, extreme_edge, …)  ← independent of UNDER
```

**Consequence:** every OVER **early + pre-inference + FV-phase** gate is an
*implicit precondition* on UNDER. UNDER is only ever considered on ticks where
the OVER side already cleared inning/ask/spread/ask-jump, min-current-total,
runs-pace, runs-needed, close-game, inn5/6 dead zones, **blowout**, FV
saturation, and Stage-2 suppression.

This is asymmetric and partly **backwards** for UNDER (see Limitations): e.g. a
blowout *suppresses* scoring (good for UNDER), but `gate_blowout` stops the tick
entirely, so UNDER never sees blowout spots. OVER's post-FV gates
(extreme_edge, fv_ask_gap, sp_era, dedup) do **not** constrain UNDER.

---

## UNDER's own gate stack · `_maybe_emit_under_candidate`

UNDER FV is the complement of the OVER raw FV run through the **UNDER
calibrator**: `under_fv = under_calibrator(1 − over_fv_raw)`. The UNDER ask /
bid come from the paired `under_no` book; `under_edge = under_fv − under_ask`.
Stage-2 / Stage-3 deltas and base FV are the negated / complemented OVER values.

If the `under_no` book has no ask at the tick → `gate_no_under_liquidity`
(skip, coverage tracking).

The 5 **symmetric** gates run first, then `gate_min_edge` last. First to fire
stops UNDER for the tick.

| # | reason | Fires when | Default(s) | Mirror of |
|---|---|---|---|---|
| U1 | `gate_under_min_inning` | line < 8.5: `inning < 4`; line ≥ 8.5: `inning < 5` | `under_min_inning=4`, `under_min_inning_high_line=5` | OVER gate 1 (variance reduction — symmetric) |
| U2 | `gate_under_min_entry_ask` | `under_ask < 0.55` (std) / `< 0.60` (high line) | `under_min_entry_ask=0.55`, `under_min_entry_ask_high_line=0.60` | OVER gate 3 (thin-book guard) |
| U3 | `gate_under_max_base_fv` | `under_base_fv ≥ 0.99` | `under_max_base_fv=0.99` | OVER gate 8f (FV saturation / phantom no-score) |
| U4 | `gate_under_extreme_edge` | `under_edge > 0.22` | `under_extreme_edge_max=0.22` | OVER `gate_extreme_edge` (TR19 phantom protection, symmetric) |
| U5 | `gate_under_fv_ask_gap` | `under_edge > 0.26` AND `inning ≥ 7` | `under_fv_ask_gap_max=0.26`, `under_fv_ask_gap_min_inning=7` | OVER gate 8g (late large-gap = market disagreement) |
| — | `gate_min_edge` | `under_edge < min_edge` | `min_edge = edge_threshold = 0.15` | OVER `gate_min_edge` (no ask-edge ramp applied to UNDER) |

All UNDER thresholds default to their OVER counterpart (`DEFAULT_UNDER_* =
DEFAULT_*`) and are independently overridable via `--under-min-inning`,
`--under-min-entry-ask`, `--under-max-base-fv`, `--under-extreme-edge-max`,
`--under-fv-ask-gap-max`, `--under-fv-ask-gap-min-inning`,
`--under-min-inning-high-line`, `--under-min-entry-ask-high-line`.

### Decision tags (candidate row `decision` / `decision_reason`)
- `shadow_under` / `shadow_under_gates_pass` — `--under-mode shadow`; row logged, **no bet**.
- `paper_under` / `paper_under_gates_pass` — `--under-mode paper`; paper `BetRecord(side="under")` placed.
- `live_under` / `live_under_gates_pass` — `--under-mode live`; **real CLOB order** on `under_no` token.
- `gate_no_under_liquidity` — no UNDER ask at this tick.
- `gate_under_*` / `gate_min_edge` — UNDER gate skip.

---

## Execution (`--under-mode`)

| mode | candidate row | bet placed | engine |
|---|---|---|---|
| `off` (default) | none | none | both |
| `shadow` | yes | none | both |
| `paper` | yes | paper `BetRecord(side="under")` (filled-at-limit) | both |
| `live` | yes | **real limit BUY on `under_no` token** | live engine only |

Live UNDER reuses the **same** side-parameterized `place_bet(side="under")` as
OVER, so it inherits identical machinery:
- **Token routing:** order posts on `market.under_token_id`; the bet record
  carries `under_token_id`, and all lifecycle (fill poll, trade-history
  recovery, orphan reconciliation) routes via `models.bet_traded_token_id(bet)`.
- **Budget / caps:** shared daily budget, per-game cap, and max-open-orders
  with OVER; **correlated-line cap applies per side** (UNDER counts only UNDER).
- **Executability:** a live UNDER order settles **only if it actually filled**
  (`placement_mode == "live"` requires `order_status == "filled"`). Paper UNDER
  stays filled-at-limit. (This closed a prior bug where `side==under` was always
  treated as executable, which would fabricate P&L on unfilled live orders.)
- **Sizing:** flat or Kelly, same as OVER. EV-policy gate is **skipped** for
  UNDER (the EV artifact is an OVER-only score-event model).
- **Settlement:** UNDER wins when `final_total < line` (OVER wins `> line`).
  MLB lines end in .5 → no pushes.

### Early-exit cancels are OVER-only
`gate_fv_decay` and `gate_ask_reversal` early-cancel logic **does not run for
UNDER** orders. Live UNDER orders rest until **fill / game-final /
stale-timeout (3h)**. This is intentional: it (a) avoids comparing an
OVER-derived recomputed FV against an UNDER limit, and (b) maximizes the real
fill data this posture exists to gather.

---

## Deferred / not-yet-implemented UNDER gates

The asymmetric OVER gates are **NOT** mirrored to UNDER because they flip
direction or need UNDER-specific design. Several currently act *against* UNDER
only as OVER preconditions (above), not as UNDER-tuned gates:

| OVER gate | Why deferred for UNDER |
|---|---|
| `gate_runs_pace` | A slow pace is *bullish* for UNDER, not bearish. |
| `gate_runs_needed_max` | Distance-to-line logic inverts for UNDER. |
| `gate_close_game_runs_needed` | Defense-first close games *favor* UNDER. |
| `gate_inning5/6_runs_needed` | Scoring-lull innings *favor* UNDER. |
| `gate_blowout` | Blowout shut-out *suppresses* scoring → *good* for UNDER. |
| `gate_stage2_suppression` | Park/weather suppression is *bullish* for UNDER. |
| `gate_sp_era` (pitcher boost) | Elite pitching *favors* UNDER (opposite sign). |
| ask-edge ramp on `min_edge` | Not applied to UNDER `min_edge`. |

Designing the UNDER-specific versions of these is the main follow-up once paper
+ live UNDER data accumulates.

---

## Limitations & caveats (read before trusting UNDER)

1. **UNDER calibrator is flagged `unreliable_pre_refit`.** Every UNDER row is
   stamped `shadow_under_calibration_status="unreliable_pre_refit"`. Debut day
   (2026-05-22 DET@BAL O7.5) had model P(under)=0.73 vs an 11-run final; 17
   emitted rows would have been ~−$170 if live. UNDER FV is an *estimate*.
2. **Precondition asymmetry (above):** UNDER only sees ticks the OVER pre-FV
   stack allowed, which filters out several spots that are structurally *good*
   for UNDER (blowouts, dead zones, suppressed environments).
3. **Not B4-validated.** The B4 milestone (≥60 sessions, ≥150 settled UNDER
   paper bets, ROI>0, |calibration delta|≤5pp, drift<3/7d) is the intended
   gate for live UNDER and was **NOT met** when live shipped — this is a
   deliberate data-gathering decision, not a green light. The `M_under_paper`
   paper fleet preset accumulates B4 evidence in parallel at zero risk.
4. **Paper P&L overstates** (assumes fill-at-limit). The real value of live
   UNDER is the fill data; judge ROI on `placement_mode == "live"` rows.
5. **Thin liquidity:** `under_no` books are often one-sided →
   `gate_no_under_liquidity` skips are common; live fills may not materialize.

---

## How to run

```bash
# Real money: OVER + UNDER live (keep daily budget low while gathering)
python scripts/trading/real_trader.py ... --under-mode live

# No-risk paper research in parallel (13-config fleet incl. M_under_paper)
python scripts/trading/launch_parallel_engines.py
```

To print live UNDER thresholds, read `signal_config.py` `DEFAULT_UNDER_*` or the
session JSON `params` block. Do not trust hardcoded values here if they
disagree with the code.
