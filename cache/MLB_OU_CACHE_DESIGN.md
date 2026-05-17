# MLB O/U Cache Design (Stage 1)

## Purpose

Build an empirical fair-value cache for MLB total-runs over/under markets, analogous to NHL Stage 1, but with baseball-native game state dimensions.

The cache is designed for live lookup during games and to reduce dependence on a generic fallback model.

## State Model

Baseball game state is keyed by:

- `away_runs`
- `home_runs`
- `inning_bucket` (1-9, plus `10` for extras)
- `half` (`T` or `B`)
- `outs` (0,1,2)
- `bases` (bitmask 0-7)

Bitmask encoding:

- bit 0 (`1`): runner on 1st
- bit 1 (`2`): runner on 2nd
- bit 2 (`4`): runner on 3rd

Examples:

- `0_0_1_T_0_0` = top 1st, 0 outs, bases empty, score 0-0
- `3_2_7_B_1_5` = bottom 7th, 1 out, runners on 1st+3rd, away 3 home 2

## Sampling Strategy

Each sample is taken **before each plate appearance** (each `allPlays` entry in MLB live feed):

1. Record current state (score, inning/half, outs, bases).
2. Update state from play result (`awayScore/homeScore`, `count.outs`, `postOnFirst/Second/Third`).
3. Continue through all plays.

This matches baseball’s discrete-event structure better than time-based snapshots.

## Supported O/U Lines

Default lines:

- `6.5`, `7.5`, `8.5`, `9.5`, `10.5`, `11.5`

Internally mapped to thresholds:

- over `L.5` => final total runs `>= L+1`

## Cell Inclusion / Reliability

Each state cell stores:

- `n`: number of unique games containing this state (independent support)
- `n_samples`: total sampled plate-appearance snapshots in this state

Cell is included only if:

- `n >= MIN_GAMES` (default 40)
- current combined runs `<= MAX_COMBINED` (default 20, controls sparse blowout tails)

## Fallback Model for Missing States

For states not meeting support threshold:

1. Estimate expected remaining runs (`lambda`) from historical samples using phase key:
   - `(inning_bucket, half, outs, bases)`
2. Compute raw Poisson `P(remaining_runs >= needed_runs)`.
3. Store a calibration table learned from low-support states:
   - key: `(line, inning_bucket, half, outs, needed_runs)`
   - method: logit-delta with Bayesian shrinkage

This mirrors NHL fallback calibration philosophy while adapting to baseball state geometry.

## Output Schema

Output file: `baseball/cache/mlb_ou_cache.json`

Top-level sections:

- `meta`: build info, thresholds, support settings
- `poisson_calibration`: calibration table + hyperparameters
- `cells`: keyed state probabilities

Per-cell fields:

- `n`, `n_samples`
- empirical probabilities for each line key (e.g. `o85`, `o95`)
- Poisson references (e.g. `po85`, `po95`)
- `lam`: expected remaining runs for that phase
- `label`: human-readable state label

## Why this design

- Uses inning/half/outs/bases instead of elapsed time.
- Captures major MLB run-scoring context with manageable sparsity.
- Keeps independent support guardrails (`n`) while still leveraging dense samples (`n_samples`).
- Provides a calibrated fallback path for live states outside strong empirical support.
