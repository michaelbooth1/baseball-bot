# Live-pause continuity: keep a live-root session flowing every day

**Standing rule:** the moment real-money live trading is paused (paper-only
week, outage, manual stop), start the **dry-run continuity engine the same
day**. Do not wait.

```
python scripts/trading/live_engine.py --dry-run --no-startup-refresh
```

Run it once per day for every day live is paused, alongside the fleet
(`launch_parallel_engines.py`, which owns the daily refresh).

## Why this is not optional

The canonical learning loop is fed from the **live root**
(`data/live_trading/sessions/<date>_session.json` + the live-root candidate
universe). Several daily-review blocks and the concept-drift report read
only that root and degrade or go blind without a session for the day:

- **`concept_drift_health` (PSI)** compares a trailing 7-day current window
  against a 30-day baseline. It needs `min_rows_per_feature = 30`. With no
  live-root rows the current window collapses and every feature reports
  `insufficient_data` — so the question "is the calibrator trained on a
  distribution that's no longer current?" becomes **unanswerable**.
- The **calibration opportunity training table**, loss-attribution, and the
  session-dependent daily-review blocks all expect a live-root session
  (see [Hygiene #6 / #7 in ROADMAP.md](../../ROADMAP.md)).

The dry-run engine places **no orders** (`--dry-run`) and skips the startup
refresh (`--no-startup-refresh`, because the fleet already owns it), so it is
side-effect-free except for the one thing we need: a live-root candidate
universe + session file for the day.

## What went wrong (the incident this rule prevents)

Real-money fills stopped after **2026-06-06**. The dry-run continuity engine
was not started until **2026-06-11** — a **4-day gap (06-07 → 06-10)** with
no live-root session at all. Consequence: by 2026-06-13 the concept-drift
current window held only **8 rows** (2026-06-06 → 06-12), so PSI was
`insufficient_data` on every feature precisely when the calibrator-enforce
"muting winners" alert was asking us to cross-check stage2 / team_offense
PSI. The continuity engine should have started 06-07, the same day live went
dark.

## Verify it's working

1. **Session exists for today:** `data/live_trading/sessions/<date>_session.json`
   is present (mode will read `dry_run`).
2. **Concept-drift window is filling:** in the daily-review JSON,
   `concept_drift_health.current_window.n_rows` climbs day over day and
   crosses 30 within ~a week of continuous running; feature verdicts move
   off `insufficient_data`.
3. **No `Session-missing:` warning** leads the daily-review Notes block
   (the Hygiene #7 guard stamps this when the live-root session is absent).
