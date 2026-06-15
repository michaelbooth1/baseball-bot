# Ship spec (QUEUED): market-anchored-alpha runtime shadow

**Status:** queued 2026-06-15. Not yet implemented — this is the ready-to-
execute spec. It touches the **live signal pipeline** (shadow only, no
decision change), so it needs operator sign-off before merging, ideally on
live re-entry rather than mid paper-only week.

> **⚠️ PREMISE WEAKENED 2026-06-15 (tape layer).** This ship was motivated
> by the shadow-CLV `ADVERSE_SELECTION` verdict read as a *market-side*
> residual. The tape / real-trade layer then showed the adverse drift is
> **CHASING, not informed flow**: 97.7% of placed bets had a FLAT tape at
> signal and **0 of 108** adverse-drift losses had real net selling against
> us (`tape_subverdict = CHASING`). So the strategic justification for this
> model — "respond to informed flow" — does **not** hold; the right response
> is cheap entry-timing / liquidity-aware execution (ROADMAP Hygiene #12),
> not a market-anchored model. What survives independently is the narrow,
> OOS-positive `no_score_drift` + `mid_no_vig` *calibration* improvement —
> keep this spec only as that modest calibration lever, **de-prioritized**
> below Hygiene #12. Do not build it as the answer to the residual.

**Why:** the T1 shadow-CLV collector found the selection-driven residual is
**ADVERSE_SELECTION** (≈62% of losses drift away from us within 2 min — the
market re-prices faster than we do). The lever that can close a market-side
residual is a market-anchored model. The
`calibration_market_anchored_alpha` walk-forward already found one OOS-
positive arm — **`no_score_drift` anchored to `mid_no_vig`** (val-selected
ROI +0.38, profit +$11.5, **CI [0.47, 20.37] excludes 0**); `score_event`
is negative on both anchors and the `ask` anchor's CI includes 0. This ship
puts that one arm into the runtime in **shadow** so live-fire evidence
accrues, and it **unblocks T8** (the fleet arm is a config flag once the
mode exists).

## Evidence gate (family-restricted by construction)

Wire **only** `no_score_drift`, **only** the `mid_no_vig` anchor. Explicitly
**exclude `score_event_transition`** (OOS-negative — anchoring it would
hurt). The model artifacts already refresh daily:
`data/analysis_output/calibration_market_anchored_alpha/by_family/
no_score_drift_market_anchored_alpha_model.json`.

The model is a `fixed_offset = logit(market_price)` + learned residual over
~50 features (state-value, market microstructure, FV-chain residuals). So:

```
market_anchored_fv = sigmoid( logit(anchor_price) + residual(features) )
```

Most `selected_feature_columns` are already computed per candidate (the same
fields the candidate row logs). The runtime needs a model-application helper
that loads `preprocessor` + `weights` from the JSON and predicts.

## The shadow contract (mirror Stage-1 Alt-A shadow exactly)

Pattern to copy: `--stage1-shadow-empirical-mode {off,shadow}` →
`trade_args.stage1_shadow_empirical_mode` → `SignalEngine` computes
`fair_value_alt_empirical` per candidate and **logs it alongside production
FV with NO decision change** (`signal_engine.py:~336`,
`signal_pipeline_gates_post_fv.py`). Replicate that for market-anchored
alpha:

- **Compute** `fair_value_market_anchored` for `no_score_drift` candidates
  only, when mode = `shadow`.
- **Log** it + `market_anchored_alpha_applied` + `market_anchored_anchor`
  (`mid_no_vig`) on every candidate row. **No gate/decision reads it.**

## Touch points (mirror the Stage-1 shadow 7-file ship)

| File | Change |
|---|---|
| `scripts/trading/market_anchored_alpha_runtime.py` (NEW) | Load per-family model JSON (preprocessor + weights + `selected_feature_columns`), assemble the feature vector from candidate context, apply offset + residual, return `market_anchored_fv`. The one piece of new core logic. |
| `signal_config.py` | `--market-anchored-alpha-mode {off,shadow}` CLI flag + `DEFAULT_MARKET_ANCHORED_ALPHA_MODE = "off"`. |
| `signal_engine.py` | Load the `no_score_drift` model at init (guarded: skip if artifact missing); read `trade_args.market_anchored_alpha_mode`. |
| `signal_pipeline_gates_post_fv.py` (or `signal_pipeline.py`) | At post-FV, when shadow + family is `no_score_drift`, call the runtime helper and attach the shadow fields. |
| `live_engine_cli.py` + `live_engine.py` | CLI flag + `trade_args` bridge. |
| `live_engine_overrides.py` | Override-file route (`market_anchored_alpha_mode`) so it toggles without a CLI change, like `stage1_shadow_empirical_mode`. |
| `session_serialization.py` + the candidate/unified-table row plumbing | Persist + propagate `fair_value_market_anchored` / `market_anchored_alpha_applied` / `market_anchored_anchor` so the daily review + training table see them (mirror the 5 Stage-1 Alt-A shadow fields). |

## Daily-review shadow surface

New `_market_anchored_alpha_shadow_health` block (mirror
`_stage1_shadow_override_health`): on settled `no_score_drift` candidates,
compare `fair_value_market_anchored` vs production FV vs outcome (bias /
brier / would-it-have-changed-the-bet), so the operator sees whether the
anchored FV is actually better before any promotion. Cross-reference the
T1 shadow-CLV `by_raw_fv_band` — the anchored FV should most help where the
market drifts adverse.

## Promotion gate

After ≥30 `no_score_drift` shadow sessions, promote to a **decision** lever
only if `fair_value_market_anchored` beats production on OOS bias + brier on
the live-fire sample (not just the walk-forward), via the usual
`promote.py` + override-file route. Until then it stays pure observability.

## Unblocks T8

Once `--market-anchored-alpha-mode` exists, add the fleet arm:

```python
"R_market_anchored_nsd": [  # A_current + the shadow mode, nothing else
    *A_current_flags,
    "--market-anchored-alpha-mode", "shadow",
],
```

The paired-delta block then accrues live-fire evidence on the anchored FV
vs A_current automatically. (Do NOT add this arm before the flag exists — it
would fail arg parsing.)

## Test plan

- `market_anchored_alpha_runtime`: model load, feature assembly from a
  candidate dict, `sigmoid(logit(anchor)+residual)` math, family gating
  (returns None / skips for `score_event`), missing-artifact fail-open.
- Pipeline: shadow mode attaches the fields on `no_score_drift` only; off
  mode is a no-op; **no decision change** in either (golden-row test).
- Daily-review block: bias/brier comparison + alert thresholds.

## Risks / caveats

- **Per-tick inference cost**: the model spans ~50 features; assembling +
  predicting on every `no_score_drift` candidate adds runtime work. Profile;
  cache the loaded model; consider computing only for candidates that reach
  the FV phase (as Stage-1 shadow does).
- **Feature-assembly fidelity**: the runtime must reproduce the training
  feature set exactly (names + preprocessing) or the residual is garbage.
  Reuse the candidate row's already-logged fields as the source of truth.
- **Keep `score_event` excluded** — it is OOS-negative. Family gating is a
  correctness requirement, not an optimization.
- Effort: **M–L** (≈ the Stage-1 Alt-A shadow ship + the new model-apply
  helper). Sequence after live re-entry so the shadow evidence is on real
  fills, not paper.
