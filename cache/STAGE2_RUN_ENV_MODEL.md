# MLB Stage-2 Run-Environment Residual Model

## Objective

Stage-1 already captures game-state effects (`score + inning/half + outs + bases`).
Stage-2 adds **run-environment residual calibration** for exogenous context that Stage-1 does not model directly:

- park
- temperature bucket
- wind bucket
- park x wind interaction

This is designed to improve O/U fair values while remaining conservative and calibration-first.

## Core Formula

For each line `L`:

1. Start with Stage-1 probability `p1(L)` from cache/fallback.
2. Compute residual delta by feature buckets:
   - `delta = w_park * d_park + w_temp * d_temp + w_wind * d_wind + w_park_wind * d_park_wind`
3. Clamp `delta` to a global cap.
4. Apply in logit space:

`p2(L) = sigmoid(logit(p1(L)) + delta)`

5. Enforce monotonicity across lines:

`P(O6.5) >= P(O7.5) >= ...`

## How Deltas Are Learned

Deltas are learned as **shrunken logit residuals** relative to Stage-1 outputs:

- Collect plate-appearance samples where a Stage-1 state cell exists.
- For each bucket + line, aggregate:
  - `n`
  - `hits` (final game total cleared line)
  - `raw_sum` (sum of Stage-1 probabilities for that line)
- Compute:
  - `raw_mean = raw_sum / n`
  - `emp_rate = hits / n`
  - `emp_shrunk = (hits + prior_n * raw_mean) / (n + prior_n)`
  - `delta = logit(emp_shrunk) - logit(raw_mean)`
- Apply family-specific min-sample thresholds and per-family delta caps.

This keeps Stage-2 anchored to Stage-1 and avoids aggressive over-corrections.

## Weight Tuning

Weights for each line are tuned on holdout seasons by minimizing validation Brier score.
If best improvement is below a minimum threshold, weights are set to zero for that line.

This enforces "no forced complexity" and reduces noise-fitting risk.

## Artifacts

- Builder script:
  - `baseball/build_mlb_stage2_run_env.py`
- Runtime model loader/applier:
  - `baseball/stage2_run_env_model.py`
- Output model:
  - `baseball/cache/mlb_stage2_run_env.json`

