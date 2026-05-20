# FV recalibration: band-gated calibrator enforce (2026-05-19)

This doc records the runtime change shipped 2026-05-19 in response to
the FV overconfidence audit. Two paths were considered; one was shipped,
the other is deferred to a scoped follow-up.

## What changed

`DEFAULT_PROB_CALIBRATION_MODE` flipped from `shadow` to `enforce` in
[signal_config.py](../../scripts/trading/signal_config.py), AND a new
default `DEFAULT_PROB_CALIBRATION_ENFORCE_MIN_RAW = 0.90` band-gates
the enforce. Together:

- raw FV >= 0.90: calibrator output (Platt-scaled) replaces raw FV
- raw FV < 0.90: raw FV is kept; the calibrator still runs and is
  logged in the per-candidate diag for observability (`calibrated_prob`,
  `delta`, `below_min_raw_kept_raw: true`, `applied: false`)

Operators can override the threshold via
`--prob-calibration-enforce-min-raw <float>`. Set to `0.0` to enforce
across the full FV range (the original enforce behavior).

## Why band-gated and not all-or-nothing

The 2026-05-19 audit pulled 1,376 settled OVER predictions from
`data/analysis_output/calibration/signal_win_calibration_predictions.jsonl`
and binned by raw FV:

| Raw FV bin | n | raw avg | actual hit | raw gap | calibrated avg | cal gap |
|---|---|---|---|---|---|---|
| [0.50,0.60) | 111 | 0.55 | 0.41 | +13pp | 0.47 | +5pp |
| [0.60,0.70) | 110 | 0.65 | 0.46 | +19pp | 0.50 | +4pp |
| [0.70,0.80) | 186 | 0.75 | 0.55 | +20pp | 0.58 | +3pp |
| [0.80,0.85) | 86 | 0.82 | 0.73 | +9pp | 0.63 | **-11pp** |
| [0.85,0.90) | 102 | 0.88 | 0.82 | +5pp | 0.66 | **-16pp** |
| [0.90,0.95) | 223 | 0.93 | 0.78 | +14pp | 0.71 | -7pp |
| [0.95,1.00) | 487 | 0.97 | 0.70 | **+28pp** | 0.75 | +5pp |

Raw FV is severely overconfident at [0.95,1.00) — the band the bot
bets most often — and the Platt calibrator correctly pulls 0.97 down
to 0.75 (very close to realized 0.70). A global enforce flip would
capture this but would also pull the [0.80,0.90) mid-band down from
~0.85 to ~0.66, where realized is actually ~0.82. The calibrator is
*under*-confident there by 10-16pp.

Band-gating at 0.90 captures the +28pp correction on the dangerous
tail without amputating mid-band bets the model gets roughly right.

## Effect on the CLE@DET 7.5 bet (today's loss that triggered the audit)

- `raw_prob = 0.979` (>= 0.90 threshold, so calibrator now applies)
- Platt-scaled estimate: ~0.78
- Decision ask = 0.80 → edge = -0.02
- **Bet would not have placed** (edge below threshold)

In the audit historical window, ~487 settled predictions sat in the
[0.95,1.00) raw band. Many will skip under the new gate. That's
expected and desirable — they were the worst-EV bets.

## Effect time

Calibration mode + threshold are read by `SignalEngine.__init__` from
`trade_args`. They take effect on **next engine boot**, like other
non-hot-reload levers (see [promotion_lag.md](./promotion_lag.md)).

The session JSON's `params.prob_calibration_mode` and
`params.prob_calibration_enforce_min_raw` columns are populated for
every session so you can confirm the new defaults were picked up.

## Rollback

Either:

1. Pass `--prob-calibration-mode shadow` on the live engine CLI
   (reverts immediately to pre-change behavior on next boot).
2. Pass `--prob-calibration-enforce-min-raw 0.0` to enforce across
   the entire FV range (the original enforce design).
3. Revert the two defaults in
   [signal_config.py](../../scripts/trading/signal_config.py):
   `DEFAULT_PROB_CALIBRATION_MODE = "shadow"` and
   `DEFAULT_PROB_CALIBRATION_ENFORCE_MIN_RAW = 0.0`.

There is no cache to revert. The calibrator artifact at
`data/analysis_output/calibration/signal_win_calibration.json` is
unchanged; only the runtime's decision to *apply* it is gated.

## Why Alt-A was NOT promoted

The audit also surfaced a second issue: the production Stage-1 cache
is built in `--smoothing-mode poisson` (the default in
[build_mlb_ou_cache.py](../../cache/build_mlb_ou_cache.py)), and
Alt-A's `empirical_when_available` mode is built into a separate
staging path
(`cache/mlb_ou_cache_alt_a.staging.json`) but never promoted. For the
CLE@DET cell `4_2_4_T_0_0` (n=112 exact samples), Poisson gives 0.982
while empirical gives 0.893 — a 9pp gap that the cache's well-supported
empirical estimate already corrects.

A global Alt-A flip would help most cells but was **not shipped
2026-05-19** because the existing
`build_stage1_shadow_override_report.py` cohort breakdown flagged a
**-23.8pp regression on the `inning>=8` cohort** (n=7, noisy but
directionally clear). The operationally correct move is **scoped**
Alt-A application — apply empirical-when-available only on cohorts
where the shadow report shows durable improvement.

See the roadmap entry "Scoped Alt-A enforce" for the design.

## Open items to revisit

These came out of the audit but are not in this ship — they're
tracked in `ROADMAP.md`:

1. **Scoped Alt-A enforce** — cohort-aware empirical override (replaces
   the existing all-or-nothing build flag with per-cohort gating).
2. **Line 5.5 disaster** — raw FV >= 0.90 on line 5.5 has realized hit
   rate 51% (n=92). Even with calibrator-enforce, this is the worst
   slice. Consider a per-line stake dampener or a hard ceiling.
3. **Mid-band calibrator under-confidence** — at raw FV [0.80,0.90)
   the Platt calibrator pulls too aggressively. Refit per-line or with
   isotonic might tighten this. Currently the band-gate works around it
   by simply not applying the calibrator there.
4. **Replace Poisson smoothing with a fatter-tailed alternative** —
   the gap is structural, not sampling noise. Negative-Binomial fit
   per cell (or per phase λ) would shorten the tail. Bigger model
   change; ship after the cheap wins prove out.
