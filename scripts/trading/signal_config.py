#!/usr/bin/env python3
"""
signal_config.py -- Trade defaults and CLI parsing for SignalEngine.

Extracted from signal_engine.py to keep core engine logic focused and make
future refactors safer. Behavior is intentionally unchanged.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Tuple

PROJECT_DIR = Path(__file__).resolve().parents[2]

# Add monitor path so we can parse monitor args after trade args.
sys.path.insert(0, str(PROJECT_DIR / 'scripts' / 'monitor'))
from monitor_mlb_polymarket_ou import parse_args as monitor_parse_args  # noqa: E402

# ---------------------------------------------------------------------------
# Constants / defaults
# ---------------------------------------------------------------------------
DEFAULT_CACHE_PATH = PROJECT_DIR / "cache" / "mlb_ou_cache.json"
DEFAULT_PAPER_ROOT = PROJECT_DIR / "data" / "paper_trading"
DEFAULT_PROB_CALIBRATION_PATH = (
    PROJECT_DIR / "data" / "analysis_output" / "calibration" / "signal_win_calibration.json"
)
# Phase A5 (2026-05-19): UNDER-side probability calibrator artifact.
# Trained separately (see scripts/analysis/calibrate_signal_probabilities.py
# --side under). Loaded by the engine when --under-emission-mode is
# `shadow` so each emitted UNDER candidate's FV passes through the same
# calibration pipeline OVER uses.
DEFAULT_PROB_CALIBRATION_UNDER_PATH = (
    PROJECT_DIR
    / "data"
    / "analysis_output"
    / "calibration"
    / "signal_win_calibration_under.json"
)
# Phase A5 (2026-05-19): UNDER candidate emission mode.
# `off` (default): the live engine does not emit UNDER candidates;
# UNDER analysis runs only offline against the OVER candidate's
# complement, which carries selection bias.
# `shadow`: alongside every OVER candidate that reaches the FV phase,
# the engine emits a sibling UNDER candidate row to the candidate
# log. UNDER bets are NEVER placed (paper or live) -- pure logging
# so the operator can validate UNDER signal quality before the
# eventual paper-mode flip (B4 in the roadmap).
DEFAULT_UNDER_EMISSION_MODE = "off"
UNDER_EMISSION_MODES = ("off", "shadow")
DEFAULT_EDGE_THRESHOLD           = 0.14   # [TR20] lowered from 0.15 for Stage-3 v2 model recalibration
DEFAULT_EDGE_THRESHOLD_HIGH_LINE = 0.15   # [TR20] lowered from 0.16 for Stage-3 v2 model recalibration
DEFAULT_JUMP_THRESHOLD           = 0.06   # min ask rise over lookback window
DEFAULT_MAX_SPREAD               = 0.20   # ignore if book spread is wider than this
DEFAULT_MIN_INNING               = 4      # don't bet before inning 4 (7.5 lines)
DEFAULT_MIN_INNING_HIGH_LINE     = 5      # [TR3] don't bet before inning 5 for lines >= 8.5
DEFAULT_HIGH_LINE_CUTOFF         = 8.5    # [TR3] float(line) >= this triggers high-line rule
DEFAULT_MIN_ENTRY_ASK            = 0.55   # [TR3] raised from 0.45 â€” skip thin/noisy books
DEFAULT_MIN_ENTRY_ASK_HIGH_LINE  = 0.60   # [TR5] stricter ask floor for lines >= high_line_cutoff
DEFAULT_CONFIRMATION_TICKS       = 3      # [TR2] ask must stay elevated this many ticks
DEFAULT_STAKE                    = 100.0  # notional dollars per paper bet
DEFAULT_STABLE_WINDOW            = 35     # ticks of stable ask before committing baseline
DEFAULT_LOOKBACK                 = 5      # ticks to look back for jump measurement
DEFAULT_COOLDOWN_TICKS           = 50     # ticks after a bet before new baseline accepted
DEFAULT_EVENT_DEDUP_SECS         = 60.0   # [TR2] seconds between bets on same game_pk (same event)
DEFAULT_MIN_CURRENT_TOTAL        = 4      # [TR3] min away+home score before betting
DEFAULT_MIN_CURRENT_TOTAL_RELAX_ENABLED = True
DEFAULT_MIN_CURRENT_TOTAL_RELAX_FLOOR = 3
DEFAULT_MIN_CURRENT_TOTAL_RELAX_INNING = 4
DEFAULT_MIN_CURRENT_TOTAL_RELAX_ASK_MIN = 0.60
DEFAULT_MIN_CURRENT_TOTAL_RELAX_MAX_LEAD = 4
DEFAULT_MIN_CURRENT_TOTAL_RELAX_MAX_RUNS_NEEDED = 4.5
DEFAULT_INNING_DEDUP_GAP         = 2      # [TR4] reduced from 3 â€” allows re-entry after 2 innings
DEFAULT_INNING_DEDUP_EDGE_GAP    = 0.05   # [TR3] new bet edge must exceed last by this margin
DEFAULT_RUNS_NEEDED_MAX          = 3.5    # [TR10] lowered from 4.0 â€” rn=3.5 bucket: 3 bets 1W/2L -40.5% ROI
DEFAULT_MIN_CLOSE_GAME_RN        = 4.0    # [TR6] in close games (lead<2), skip if rn >= this
DEFAULT_INN5_RN_MAX              = 2.5    # [TR10] lowered from 3.0 â€” inn5+rn=2.5: 7 bets 2W/5L 29% WR -$375
DEFAULT_INN6_RN_MAX              = 2.5    # [TR9] inning 6 setup-reliever dead zone: skip if rn >= this
                                          # Backtest 2021-25: inn6+rn=2.5 â†’ 66.6%T/60.4%B WR (1,569 bets)
                                          # vs inn6+rn=1.5 â†’ 79.8%T/74.0%B (above break-even)

# [TR12] Phantom-run protection gates.
# When the Stage-1 Poisson model hits its saturation ceiling (base_fv ~= 1.0),
# the inferred game state is near-certainty â€” almost always because an inferred
# run pushes the total to exactly the line in a late inning with 0 outs.
# This is the primary fingerprint of a phantom API score update: market
# participants with faster feeds are selling into our signal at 0.55-0.65
# while our model says 0.98-1.0.  Session data (2026-04-21): STL@MIA O8.5
# base_fv=1.0, market ask=0.58, filled, lost -$31.24.
DEFAULT_MAX_BASE_FV            = 0.99   # [TR12â†’TR13] raised 0.98â†’0.99: 0.9814 fill was a WIN, only 1.0 is true saturation
DEFAULT_FV_ASK_GAP_MAX         = 0.26   # [TR12->TR13] lowered 0.30->0.28: PIT@TEX edge=0.294 inn=7 LOSS
# [2026-05-17] Lowered 0.28 -> 0.26: matches the operator's runtime
# setting. Risk-reducing direction (blocks MORE late-game phantom-
# score patterns where fair_value - decision_ask exceeds the cap in
# inning >= 7). Active #1 walk-forward should re-certify on the
# day-30 trigger; the change is more conservative so it CAN ONLY
# REDUCE exposure, never increase it.
DEFAULT_FV_ASK_GAP_MIN_INNING  = 7      # [TR12] â€¦but only in inning >= this (late-game phantom risk)

# [TR17, 2026-05-01] Extreme-edge phantom-run protection -- PROMOTED FROM SHADOW
# TO ENFORCED at 0.30. Cumulative live evidence (through 2026-04-30): edge > 0.25
# settled filled bets were 1W / 5L, -$117.56 realized on $140.29 stake.
#
# [TR18, 2026-05-03] THRESHOLD TIGHTENED 0.30 -> 0.22 based on the 2026-04-28
# through 2026-05-03 live window (analysis: scripts/analysis/
# analyze_window_2026_04_28_to_05_03.py). In that 6-day window the edge>0.20
# cohort was 4W / 8L, -$79.57 realized on 12 bets at avg fill price ~0.63.
# Wilson 95% upper bound on the cohort win rate is ~58%, well below the ~62%
# break-even at that fill price. The cohort dominates window P&L: cumulative
# realized loss in the 6 days was -$55.19, of which -$79.57 came from
# edge>0.20. Bets with edge<0.20 in the same window were +$24.38 (10W/1L).
# All 8 unique window losses had p_score_event_proxy = 0.000 (no ask-jump
# confirmation that an actual run scored), confirming these are systematic
# phantom-run losses rather than variance.
#
# Threshold choice (0.22 not 0.20): keeps a 2pp buffer above the empirical
# cohort boundary so bets near the model edge_threshold (0.10/0.15 for high
# lines) still pass. False-positive cost (skipping a real high-edge winner)
# remains small in expectation; false-negative cost is concentrated and large.
# Tunable via --extreme-edge-max if a future audit shifts the boundary.
DEFAULT_EXTREME_EDGE_MAX        = 0.17

# [TR14] LTP-vs-ask shadow risk tag -- not an enforced gate. Historical examples
# above 0.50 include winners/missed winners, so use this for diagnostics rather
# than blocking trades.
DEFAULT_LTP_ASK_GAP_MAX         = 0.50

# [TR13] Blowout-adjacent gate: adds a second condition to Gate 8e for games where
# the trailing team has 1 run and trails by 4+ in inning 7+.  Poisson FV is inflated
# in these states for the same reason as full blowouts: dominant bullpen, lineup
# manipulation, selection bias on which pitchers survive to inning 7.
# Live: PIT@TEX 2026-04-21 â€” 1-5 in inning 7, LOSS -$25.46.
DEFAULT_BLOWOUT_LEAD_MIN      = 6   # [TR11] existing: lead >= this AND inning >= 6
DEFAULT_BLOWOUT_ADJ_LEAD_MIN  = 4   # [TR13] adjacent: lead >= this AND inning >= 7
DEFAULT_BLOWOUT_RELAX_MAX_INNING = 7
DEFAULT_BLOWOUT_RELAX_MIN_ASK = 0.74
DEFAULT_BLOWOUT_RELAX_MAX_RUNS_NEEDED = 2.5
DEFAULT_BLOWOUT_RELAX_NEAR_LINE_RUNS_NEEDED = 0.5
DEFAULT_BLOWOUT_RELAX_NEAR_LINE_MIN_ASK = 0.55

# [TR13] Stage-2 extreme suppression gate: when the park/weather logit adjustment
# is >= 0.20 logit below zero AND we are in inning 6+, the correction likely
# underestimates total suppression.  Oracle Park (SF) and Wrigley Field (CHC,
# wind-blowing-in) are the primary cases.
# Live: PHI@CHC 2026-04-20 S2=-0.200 inn=6 LOSS; LAD@SF 2026-04-21 S2=-0.230 inn=4
#   (inn=4 is below the inning threshold â€” a P2 candidate if data accumulates).
DEFAULT_S2_SUPPRESS_MAX        = -0.20  # [TR13] gate fires when S2 delta <= this (logit)
DEFAULT_S2_SUPPRESS_MIN_INNING = 6      # [TR13] only in inning >= this

# [TR13] Stage-4 pitcher quality gate: when the current defense pitcher's ERA is below
# the threshold in early innings (<=6), scoring is harder to reach than the team-average
# Poisson model assumes.  Gate raises the minimum required edge by sp_era_edge_boost.
# Requires pitcher_cache loaded in the monitor (--pitcher-cache-path); if missing, the
# defense pitcher defaults to MLB_AVG_ERA=4.20 which is ABOVE the 3.75 threshold, so
# the gate never fires without an explicit pitcher cache.
DEFAULT_SP_ERA_THRESHOLD  = 3.75  # [TR13] ERA below this triggers the edge boost
DEFAULT_SP_ERA_MAX_INNING = 6     # [TR13] only boost in innings <= this (starter likely pitching)
DEFAULT_SP_ERA_EDGE_BOOST = 0.03  # [TR13] add this to min_edge when pitcher ERA < threshold

DEFAULT_TEAM_GAME_LOG_PATH = PROJECT_DIR / "cache" / "team_game_log.json"
DEFAULT_STAGE2_MODEL_PATH = PROJECT_DIR / "cache" / "mlb_stage2_run_env.json"

# Ask-aware min-edge ramp: tighten edge requirement as ask rises.
DEFAULT_ASK_EDGE_RAMP_ENABLED = True
DEFAULT_ASK_EDGE_RAMP_START = 0.75
DEFAULT_ASK_EDGE_RAMP_END = 0.90
DEFAULT_ASK_EDGE_RAMP_MAX_BOOST = 0.05

# Conditional gate-relax rollout controls (shadow-first, then A/B).
DEFAULT_GATE_MIN_CURRENT_TOTAL_RELAX_MODE = "shadow"  # off|shadow|enforce|ab
DEFAULT_GATE_BLOWOUT_RELAX_MODE = "enforce"           # off|shadow|enforce|ab
DEFAULT_GATE_RELAX_AB_FRACTION = 0.50

# Probability calibration rollout (artifact fitted from unified dataset).
DEFAULT_PROB_CALIBRATION_MODE = "enforce"  # off|shadow|enforce
# Band-gated enforce: only overwrite raw FV when raw >= this threshold.
# Below the threshold, the calibrator is still scored (for observability)
# but raw FV is kept (shadow-like behavior). 2026-05-19 audit found the
# Platt calibrator is well-behaved at raw>=0.90 (pulls 0.97 -> 0.75, very
# close to realized 0.70 on n=487) but over-pulls in [0.80,0.90) where
# raw is only +5pp overconfident and calibrated lands -10 to -16pp UNDER
# realized. Restricting enforce to the high-FV band captures the big EV
# correction without amputating mid-band bets the model gets ~right.
# Set to 0.0 to enforce across the whole range (the original behavior).
DEFAULT_PROB_CALIBRATION_ENFORCE_MIN_RAW = 0.90

# Shadow state-value/no-score drift diagnostics. These do not place trades.
# They collect evidence for the broader premise:
# "State-value Over trading around market overreaction to score and no-score transitions."
DEFAULT_SHADOW_NO_SCORE_DRIFT_ENABLED = True
DEFAULT_SHADOW_NO_SCORE_DRIFT_MIN_INNING = 3
DEFAULT_SHADOW_NO_SCORE_DRIFT_MAX_INNING = 8
DEFAULT_SHADOW_NO_SCORE_DRIFT_MIN_SEGMENT_AGE_SECS = 180.0
DEFAULT_SHADOW_NO_SCORE_DRIFT_MIN_SEGMENT_TICKS = 20
DEFAULT_SHADOW_NO_SCORE_DRIFT_MIN_DRAWDOWN = 0.06
DEFAULT_SHADOW_NO_SCORE_DRIFT_MIN_ASK = 0.35
DEFAULT_SHADOW_NO_SCORE_DRIFT_MAX_ASK = 0.85
DEFAULT_SHADOW_NO_SCORE_DRIFT_MAX_SPREAD = 0.12
DEFAULT_SHADOW_NO_SCORE_DRIFT_MIN_PO_EDGE = 0.10
DEFAULT_SHADOW_NO_SCORE_DRIFT_MIN_EMP_EDGE = 0.08

# Book capture: high-freq snapshots taken after every signal fires.
# Used to calibrate limit-order pricing (buying_model_1.txt).
# [TR9] Extended from 10s to 30s â€” 10s was too short for limit-order fill analysis.
DEFAULT_CAPTURE_DURATION  = 120.0  # seconds to poll after signal
# (2026-05-17) Bumped 30.0 -> 120.0. The post-signal book-capture
# analysis (`scripts/analysis/analyze_book_captures.py`) uses
# `max_elapsed=60.0` for fill-window simulation, so the previous 30s
# default truncated that analysis silently. 120s covers all current
# analysis + leaves headroom for Phase D queue-position research
# without needing a recapture campaign.
DEFAULT_CAPTURE_INTERVAL  = 1.0    # seconds between snapshots
DEFAULT_CAPTURE_DEPTH     = 5      # top-N bid/ask levels stored per snapshot
# (2026-05-17) Bumped 3 -> 5. No current analysis traverses past
# top-of-book, but Phase D market-maker work needs queue-position
# data (requires multi-level depth). Cost is 2 extra rows per
# snapshot -- trivial vs the data-utility upside.

# Shadow relaxed-gate diagnostics (metrics only; never change trade decisions).
DEFAULT_SHADOW_RELAXED_ENABLED = True
DEFAULT_SHADOW_RELAXED_MIN_CURRENT_TOTAL = 3
DEFAULT_SHADOW_RELAXED_RUNS_NEEDED_MAX = 4.0
DEFAULT_SHADOW_RELAXED_MIN_CLOSE_GAME_RN = 4.5
DEFAULT_SHADOW_RELAXED_INN5_RN_MAX = 3.0
DEFAULT_SHADOW_RELAXED_INN6_RN_MAX = 3.0
DEFAULT_SHADOW_RELAXED_BLOWOUT_LEAD_MIN = 7
DEFAULT_SHADOW_RELAXED_BLOWOUT_ADJ_LEAD_MIN = 5
DEFAULT_SHADOW_RELAXED_BLOWOUT_COND_MIN_INNING = 8
DEFAULT_SHADOW_RELAXED_BLOWOUT_COND_MAX_ASK = 0.70
DEFAULT_SHADOW_RELAXED_S2_SUPPRESS_MAX = -0.24
DEFAULT_SHADOW_RELAXED_S2_SUPPRESS_MIN_INNING = 7
DEFAULT_SHADOW_RELAXED_MAX_BASE_FV = 1.0       # Only exact-1.0 saturation is blocked
DEFAULT_SHADOW_RELAXED_MAX_BASE_FV_V2 = 0.995  # Tighter test: block only >= 0.995
DEFAULT_SHADOW_RELAXED_FV_ASK_GAP_MAX = 0.33
DEFAULT_SHADOW_RELAXED_FV_ASK_GAP_MIN_INNING = 8
DEFAULT_SHADOW_RELAXED_MIN_EDGE_OFFSET = 0.03
DEFAULT_SHADOW_RELAXED_SP_ERA_EDGE_BOOST = 0.02
# Gate 7 (runs pace): relaxed pace buffer â€” line - 2.0 instead of line - 1.5
DEFAULT_SHADOW_RELAXED_PACE_BUFFER = 2.0
# Gate 1 (inning min): relaxed inning floor â€” one inning earlier than strict
DEFAULT_SHADOW_RELAXED_MIN_INNING_OFFSET = 1
DEFAULT_SHADOW_RELAXED_MIN_INNING_HIGH_LINE_OFFSET = 1

# Fraction of original jump magnitude the ask must retain during confirmation window.
# Prevents a spike that decays substantially from still counting as a confirmed signal.
CONFIRM_RETAIN_FRACTION = 0.5

# Inning states where no active play is occurring â€” skip these
INACTIVE_INNING_STATES = {"end", "middle"}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_trade_args(argv=None) -> Tuple[argparse.Namespace, argparse.Namespace]:
    p = argparse.ArgumentParser(
        description="Paper trading engine on top of the MLB Polymarket monitor.",
        add_help=False,
        allow_abbrev=False,
    )
    p.add_argument("--edge-threshold", type=float, default=DEFAULT_EDGE_THRESHOLD,
                   help=f"Min (FV - ask) to trigger a bet (default: {DEFAULT_EDGE_THRESHOLD}).")
    p.add_argument("--edge-threshold-high-line", type=float, default=DEFAULT_EDGE_THRESHOLD_HIGH_LINE,
                   help=f"[TR5] Min edge for lines >= high-line-cutoff (default: {DEFAULT_EDGE_THRESHOLD_HIGH_LINE}).")
    p.add_argument("--jump-threshold", type=float, default=DEFAULT_JUMP_THRESHOLD,
                   help=f"Min ask rise over lookback window (default: {DEFAULT_JUMP_THRESHOLD}).")
    p.add_argument("--max-spread", type=float, default=DEFAULT_MAX_SPREAD,
                   help=f"Skip if spread wider than this (default: {DEFAULT_MAX_SPREAD}).")
    p.add_argument("--min-inning", type=int, default=DEFAULT_MIN_INNING,
                   help=f"Earliest inning for standard lines (default: {DEFAULT_MIN_INNING}).")
    p.add_argument("--min-inning-high-line", type=int, default=DEFAULT_MIN_INNING_HIGH_LINE,
                   help=f"[TR3] Earliest inning for lines >= high-line-cutoff (default: {DEFAULT_MIN_INNING_HIGH_LINE}).")
    p.add_argument("--high-line-cutoff", type=float, default=DEFAULT_HIGH_LINE_CUTOFF,
                   help=f"[TR3] Line value at/above which high-line inning rule applies (default: {DEFAULT_HIGH_LINE_CUTOFF}).")
    p.add_argument("--min-entry-ask", type=float, default=DEFAULT_MIN_ENTRY_ASK,
                   help=f"[TR3] Skip signals where ask < this (default: {DEFAULT_MIN_ENTRY_ASK}).")
    p.add_argument("--min-entry-ask-high-line", type=float, default=DEFAULT_MIN_ENTRY_ASK_HIGH_LINE,
                   help=f"[TR5] Stricter ask floor for lines >= high-line-cutoff (default: {DEFAULT_MIN_ENTRY_ASK_HIGH_LINE}).")
    p.add_argument("--runs-needed-max", type=float, default=DEFAULT_RUNS_NEEDED_MAX,
                   help=f"[TR5] Skip if (line - current_total) > this (default: {DEFAULT_RUNS_NEEDED_MAX}).")
    p.add_argument("--min-current-total", type=int, default=DEFAULT_MIN_CURRENT_TOTAL,
                   help=f"[TR3] Skip if away+home score < this at bet time (default: {DEFAULT_MIN_CURRENT_TOTAL}).")
    p.add_argument("--min-current-total-relax-enabled", dest="min_current_total_relax_enabled",
                   action="store_true", default=DEFAULT_MIN_CURRENT_TOTAL_RELAX_ENABLED,
                   help="Gate 6 conditional relax: allow inning/ask-aware pass below strict min-current-total.")
    p.add_argument("--no-min-current-total-relax", dest="min_current_total_relax_enabled",
                   action="store_false",
                   help="Disable Gate 6 conditional relax and use strict min-current-total only.")
    p.add_argument("--min-current-total-relax-floor", type=int, default=DEFAULT_MIN_CURRENT_TOTAL_RELAX_FLOOR,
                   help=f"Gate 6 relax: minimum total allowed when relax rule applies (default: {DEFAULT_MIN_CURRENT_TOTAL_RELAX_FLOOR}).")
    p.add_argument("--min-current-total-relax-inning", type=int, default=DEFAULT_MIN_CURRENT_TOTAL_RELAX_INNING,
                   help=f"Gate 6 relax: inning where relax rule applies (default: {DEFAULT_MIN_CURRENT_TOTAL_RELAX_INNING}).")
    p.add_argument("--min-current-total-relax-ask-min", type=float, default=DEFAULT_MIN_CURRENT_TOTAL_RELAX_ASK_MIN,
                   help=f"Gate 6 relax: minimum ask required for relax pass (default: {DEFAULT_MIN_CURRENT_TOTAL_RELAX_ASK_MIN}).")
    p.add_argument("--min-current-total-relax-max-lead", type=int, default=DEFAULT_MIN_CURRENT_TOTAL_RELAX_MAX_LEAD,
                   help=f"Gate 6 relax: maximum lead_abs allowed for relax pass (default: {DEFAULT_MIN_CURRENT_TOTAL_RELAX_MAX_LEAD}).")
    p.add_argument("--min-current-total-relax-max-runs-needed", type=float, default=DEFAULT_MIN_CURRENT_TOTAL_RELAX_MAX_RUNS_NEEDED,
                   help=f"Gate 6 relax: maximum runs_needed allowed for relax pass (default: {DEFAULT_MIN_CURRENT_TOTAL_RELAX_MAX_RUNS_NEEDED}).")
    p.add_argument("--gate-min-current-total-relax-mode", choices=["off", "shadow", "enforce", "ab"],
                   default=DEFAULT_GATE_MIN_CURRENT_TOTAL_RELAX_MODE,
                   help=f"Gate 6 conditional relax rollout mode (default: {DEFAULT_GATE_MIN_CURRENT_TOTAL_RELAX_MODE}).")
    p.add_argument("--confirmation-ticks", type=int, default=DEFAULT_CONFIRMATION_TICKS,
                   help=f"[TR2] Ticks ask must stay elevated before firing (default: {DEFAULT_CONFIRMATION_TICKS}).")
    p.add_argument("--event-dedup-secs", type=float, default=DEFAULT_EVENT_DEDUP_SECS,
                   help=f"[TR2] Seconds between bets on same game_pk (same event) (default: {DEFAULT_EVENT_DEDUP_SECS}).")
    p.add_argument("--inning-dedup-gap", type=int, default=DEFAULT_INNING_DEDUP_GAP,
                   help=f"[TR3] Innings that must pass before re-betting same game (default: {DEFAULT_INNING_DEDUP_GAP}).")
    p.add_argument("--inning-dedup-edge-gap", type=float, default=DEFAULT_INNING_DEDUP_EDGE_GAP,
                   help=f"[TR3] Edge improvement required to override inning dedup (default: {DEFAULT_INNING_DEDUP_EDGE_GAP}).")
    p.add_argument("--stake", type=float, default=DEFAULT_STAKE,
                   help=f"Notional dollars per paper bet (default: {DEFAULT_STAKE}).")
    p.add_argument("--lookback-ticks", type=int, default=DEFAULT_LOOKBACK,
                   help=f"Ticks to look back for jump measurement (default: {DEFAULT_LOOKBACK}).")
    p.add_argument("--cache-path", type=Path, default=DEFAULT_CACHE_PATH,
                   help=f"Path to Stage-1 cache JSON (default: {DEFAULT_CACHE_PATH}).")
    p.add_argument("--team-game-log-path", type=Path, default=DEFAULT_TEAM_GAME_LOG_PATH,
                   help=f"[Stage-3] Path to team game log JSON (default: {DEFAULT_TEAM_GAME_LOG_PATH}).")
    p.add_argument("--stage2-model-path", type=Path, default=DEFAULT_STAGE2_MODEL_PATH,
                   help=f"[Stage-2] Path to park/weather run-env model JSON (default: {DEFAULT_STAGE2_MODEL_PATH}).")
    p.add_argument("--min-close-game-rn", type=float, default=DEFAULT_MIN_CLOSE_GAME_RN,
                   help=f"[TR6] In close games (lead<2), skip if runs_needed >= this (default: {DEFAULT_MIN_CLOSE_GAME_RN}).")
    p.add_argument("--inn5-rn-max", type=float, default=DEFAULT_INN5_RN_MAX,
                   help=f"[TR8] At inning 5, skip if runs_needed >= this (default: {DEFAULT_INN5_RN_MAX}).")
    p.add_argument("--inn6-rn-max", type=float, default=DEFAULT_INN6_RN_MAX,
                   help=f"[TR9] At inning 6, skip if runs_needed >= this (default: {DEFAULT_INN6_RN_MAX}).")
    p.add_argument("--blowout-lead-min", type=int, default=DEFAULT_BLOWOUT_LEAD_MIN,
                   help=f"[TR11/TR13] Skip if trailing<=1 AND lead >= this AND inning >= 6. "
                        f"Full blowout threshold (existing Gate 8e behavior). "
                        f"(default: {DEFAULT_BLOWOUT_LEAD_MIN})")
    p.add_argument("--blowout-adj-lead-min", type=int, default=DEFAULT_BLOWOUT_ADJ_LEAD_MIN,
                   help=f"[TR13] Skip if trailing<=1 AND lead >= this AND inning >= 7. "
                        f"Blowout-adjacent: smaller lead is enough once game is in the 7th. "
                        f"(default: {DEFAULT_BLOWOUT_ADJ_LEAD_MIN})")
    p.add_argument("--blowout-relax-max-inning", type=int, default=DEFAULT_BLOWOUT_RELAX_MAX_INNING,
                   help=f"Gate 8e conditional relax: maximum inning allowed for relax pass (default: {DEFAULT_BLOWOUT_RELAX_MAX_INNING}).")
    p.add_argument("--blowout-relax-min-ask", type=float, default=DEFAULT_BLOWOUT_RELAX_MIN_ASK,
                   help=f"Gate 8e conditional relax: minimum ask required for relax pass (default: {DEFAULT_BLOWOUT_RELAX_MIN_ASK}).")
    p.add_argument("--blowout-relax-max-runs-needed", type=float, default=DEFAULT_BLOWOUT_RELAX_MAX_RUNS_NEEDED,
                   help=f"Gate 8e conditional relax: maximum runs_needed allowed for relax pass (default: {DEFAULT_BLOWOUT_RELAX_MAX_RUNS_NEEDED}).")
    p.add_argument("--blowout-relax-near-line-runs-needed", type=float,
                   default=DEFAULT_BLOWOUT_RELAX_NEAR_LINE_RUNS_NEEDED,
                   help=f"Gate 8e conditional relax: always allow near-line states at or below this runs_needed when ask floor passes (default: {DEFAULT_BLOWOUT_RELAX_NEAR_LINE_RUNS_NEEDED}).")
    p.add_argument("--blowout-relax-near-line-min-ask", type=float,
                   default=DEFAULT_BLOWOUT_RELAX_NEAR_LINE_MIN_ASK,
                   help=f"Gate 8e conditional relax: minimum ask for near-line relax pass (default: {DEFAULT_BLOWOUT_RELAX_NEAR_LINE_MIN_ASK}).")
    p.add_argument("--gate-blowout-relax-mode", choices=["off", "shadow", "enforce", "ab"],
                   default=DEFAULT_GATE_BLOWOUT_RELAX_MODE,
                   help=f"Gate 8e conditional relax rollout mode (default: {DEFAULT_GATE_BLOWOUT_RELAX_MODE}).")
    p.add_argument("--gate-relax-ab-fraction", type=float, default=DEFAULT_GATE_RELAX_AB_FRACTION,
                   help=f"Treatment fraction when gate relax mode is 'ab' (default: {DEFAULT_GATE_RELAX_AB_FRACTION}).")
    p.add_argument("--s2-suppress-max", type=float, default=DEFAULT_S2_SUPPRESS_MAX,
                   help=f"[TR13] Skip signals where Stage-2 logit delta <= this AND "
                        f"inning >= --s2-suppress-min-inning. Extreme park/weather "
                        f"suppression indicates Stage-2 correction is insufficient. "
                        f"(default: {DEFAULT_S2_SUPPRESS_MAX})")
    p.add_argument("--s2-suppress-min-inning", type=int, default=DEFAULT_S2_SUPPRESS_MIN_INNING,
                   help=f"[TR13] Minimum inning for Stage-2 extreme suppression gate. "
                        f"(default: {DEFAULT_S2_SUPPRESS_MIN_INNING})")
    p.add_argument("--sp-era-threshold", type=float, default=DEFAULT_SP_ERA_THRESHOLD,
                   help=f"[TR13] Gate 8i: if defense pitcher ERA < this AND inning <= --sp-era-max-inning, "
                        f"require edge >= min_edge + --sp-era-edge-boost. "
                        f"Disabled if pitcher cache not loaded (defaults to 4.20 avg). "
                        f"(default: {DEFAULT_SP_ERA_THRESHOLD})")
    p.add_argument("--sp-era-max-inning", type=int, default=DEFAULT_SP_ERA_MAX_INNING,
                   help=f"[TR13] Gate 8i: maximum inning for pitcher ERA edge boost (starter likely pitching). "
                        f"(default: {DEFAULT_SP_ERA_MAX_INNING})")
    p.add_argument("--sp-era-edge-boost", type=float, default=DEFAULT_SP_ERA_EDGE_BOOST,
                   help=f"[TR13] Gate 8i: additional edge required when pitcher ERA < threshold. "
                        f"(default: {DEFAULT_SP_ERA_EDGE_BOOST})")
    p.add_argument("--ask-edge-ramp-enabled", dest="ask_edge_ramp_enabled",
                   action="store_true", default=DEFAULT_ASK_EDGE_RAMP_ENABLED,
                   help="Enable ask-aware edge ramp (higher ask requires higher min edge).")
    p.add_argument("--no-ask-edge-ramp", dest="ask_edge_ramp_enabled", action="store_false",
                   help="Disable ask-aware edge ramp.")
    p.add_argument("--ask-edge-ramp-start", type=float, default=DEFAULT_ASK_EDGE_RAMP_START,
                   help=f"Ask where edge ramp begins (default: {DEFAULT_ASK_EDGE_RAMP_START}).")
    p.add_argument("--ask-edge-ramp-end", type=float, default=DEFAULT_ASK_EDGE_RAMP_END,
                   help=f"Ask where max edge boost is reached (default: {DEFAULT_ASK_EDGE_RAMP_END}).")
    p.add_argument("--ask-edge-ramp-max-boost", type=float, default=DEFAULT_ASK_EDGE_RAMP_MAX_BOOST,
                   help=f"Maximum additional min edge from ask ramp (default: {DEFAULT_ASK_EDGE_RAMP_MAX_BOOST}).")
    p.add_argument("--max-base-fv", type=float, default=DEFAULT_MAX_BASE_FV,
                   help=f"[TR12] Skip signals where Stage-1 base FV >= this threshold. "
                        f"Values near 1.0 indicate Poisson saturation / phantom run risk. "
                        f"(default: {DEFAULT_MAX_BASE_FV})")
    p.add_argument("--fv-ask-gap-max", type=float, default=DEFAULT_FV_ASK_GAP_MAX,
                   help=f"[TR12] Skip signals where (fair_value - ask) > this AND "
                        f"inning >= --fv-ask-gap-min-inning. Large late-inning gaps "
                        f"indicate market has better real-time score data. "
                        f"(default: {DEFAULT_FV_ASK_GAP_MAX})")
    p.add_argument("--fv-ask-gap-min-inning", type=int, default=DEFAULT_FV_ASK_GAP_MIN_INNING,
                   help=f"[TR12] Minimum inning for the large FV/ask gap gate. "
                        f"(default: {DEFAULT_FV_ASK_GAP_MIN_INNING})")
    p.add_argument("--extreme-edge-max", type=float, default=DEFAULT_EXTREME_EDGE_MAX,
                   help=f"[TR17] Enforced gate: skip signals where edge exceeds this threshold "
                        f"in any inning. Also logged as a shadow risk tag for diagnostics. "
                        f"(default: {DEFAULT_EXTREME_EDGE_MAX})")
    p.add_argument("--ltp-ask-gap-max", type=float, default=DEFAULT_LTP_ASK_GAP_MAX,
                   help=f"[TR14] Shadow-tag signals where |ask - ltp| exceeds this threshold. "
                        f"Diagnostics only; does not block trades. "
                        f"(default: {DEFAULT_LTP_ASK_GAP_MAX})")
    p.add_argument("--shadow-relaxed-enabled", dest="shadow_relaxed_enabled",
                   action="store_true", default=DEFAULT_SHADOW_RELAXED_ENABLED,
                   help="Enable shadow relaxed-gate diagnostics (metrics only; never affects trading decisions).")
    p.add_argument("--no-shadow-relaxed", dest="shadow_relaxed_enabled", action="store_false",
                   help="Disable shadow relaxed-gate diagnostics.")
    p.add_argument("--shadow-relaxed-min-current-total", type=int, default=DEFAULT_SHADOW_RELAXED_MIN_CURRENT_TOTAL,
                   help=f"Shadow relaxed threshold for Gate 6 current total (default: {DEFAULT_SHADOW_RELAXED_MIN_CURRENT_TOTAL}).")
    p.add_argument("--shadow-relaxed-runs-needed-max", type=float, default=DEFAULT_SHADOW_RELAXED_RUNS_NEEDED_MAX,
                   help=f"Shadow relaxed max runs_needed for Gate 8 (default: {DEFAULT_SHADOW_RELAXED_RUNS_NEEDED_MAX}).")
    p.add_argument("--shadow-relaxed-min-close-game-rn", type=float, default=DEFAULT_SHADOW_RELAXED_MIN_CLOSE_GAME_RN,
                   help=f"Shadow relaxed close-game runs_needed threshold (default: {DEFAULT_SHADOW_RELAXED_MIN_CLOSE_GAME_RN}).")
    p.add_argument("--shadow-relaxed-inn5-rn-max", type=float, default=DEFAULT_SHADOW_RELAXED_INN5_RN_MAX,
                   help=f"Shadow relaxed inning-5 runs_needed threshold (default: {DEFAULT_SHADOW_RELAXED_INN5_RN_MAX}).")
    p.add_argument("--shadow-relaxed-inn6-rn-max", type=float, default=DEFAULT_SHADOW_RELAXED_INN6_RN_MAX,
                   help=f"Shadow relaxed inning-6 runs_needed threshold (default: {DEFAULT_SHADOW_RELAXED_INN6_RN_MAX}).")
    p.add_argument("--shadow-relaxed-blowout-lead-min", type=int, default=DEFAULT_SHADOW_RELAXED_BLOWOUT_LEAD_MIN,
                   help=f"Shadow relaxed full blowout lead threshold (default: {DEFAULT_SHADOW_RELAXED_BLOWOUT_LEAD_MIN}).")
    p.add_argument("--shadow-relaxed-blowout-adj-lead-min", type=int, default=DEFAULT_SHADOW_RELAXED_BLOWOUT_ADJ_LEAD_MIN,
                   help=f"Shadow relaxed blowout-adjacent lead threshold (default: {DEFAULT_SHADOW_RELAXED_BLOWOUT_ADJ_LEAD_MIN}).")
    p.add_argument("--shadow-relaxed-blowout-cond-min-inning", type=int, default=DEFAULT_SHADOW_RELAXED_BLOWOUT_COND_MIN_INNING,
                   help=f"Gate 8e shadow-only conditional relax: only block if inning >= this (default: {DEFAULT_SHADOW_RELAXED_BLOWOUT_COND_MIN_INNING}).")
    p.add_argument("--shadow-relaxed-blowout-cond-max-ask", type=float, default=DEFAULT_SHADOW_RELAXED_BLOWOUT_COND_MAX_ASK,
                   help=f"Gate 8e shadow-only conditional relax: only block if ask < this (default: {DEFAULT_SHADOW_RELAXED_BLOWOUT_COND_MAX_ASK}).")
    p.add_argument("--shadow-relaxed-s2-suppress-max", type=float, default=DEFAULT_SHADOW_RELAXED_S2_SUPPRESS_MAX,
                   help=f"Shadow relaxed Stage-2 suppression max delta (default: {DEFAULT_SHADOW_RELAXED_S2_SUPPRESS_MAX}).")
    p.add_argument("--shadow-relaxed-s2-suppress-min-inning", type=int, default=DEFAULT_SHADOW_RELAXED_S2_SUPPRESS_MIN_INNING,
                   help=f"Shadow relaxed minimum inning for Stage-2 suppression (default: {DEFAULT_SHADOW_RELAXED_S2_SUPPRESS_MIN_INNING}).")
    p.add_argument("--shadow-relaxed-max-base-fv", type=float, default=DEFAULT_SHADOW_RELAXED_MAX_BASE_FV,
                   help=f"Shadow relaxed max Stage-1 base FV (default: {DEFAULT_SHADOW_RELAXED_MAX_BASE_FV}).")
    p.add_argument("--shadow-relaxed-fv-ask-gap-max", type=float, default=DEFAULT_SHADOW_RELAXED_FV_ASK_GAP_MAX,
                   help=f"Shadow relaxed max FV/ask gap threshold (default: {DEFAULT_SHADOW_RELAXED_FV_ASK_GAP_MAX}).")
    p.add_argument("--shadow-relaxed-fv-ask-gap-min-inning", type=int, default=DEFAULT_SHADOW_RELAXED_FV_ASK_GAP_MIN_INNING,
                   help=f"Shadow relaxed min inning for FV/ask gap gate (default: {DEFAULT_SHADOW_RELAXED_FV_ASK_GAP_MIN_INNING}).")
    p.add_argument("--shadow-relaxed-min-edge-offset", type=float, default=DEFAULT_SHADOW_RELAXED_MIN_EDGE_OFFSET,
                   help=f"Shadow relaxed edge offset for gate_min_edge (default: {DEFAULT_SHADOW_RELAXED_MIN_EDGE_OFFSET}).")
    p.add_argument("--shadow-relaxed-sp-era-edge-boost", type=float, default=DEFAULT_SHADOW_RELAXED_SP_ERA_EDGE_BOOST,
                   help=f"Shadow relaxed pitcher ERA edge boost (default: {DEFAULT_SHADOW_RELAXED_SP_ERA_EDGE_BOOST}).")
    p.add_argument("--shadow-relaxed-max-base-fv-v2", type=float, default=DEFAULT_SHADOW_RELAXED_MAX_BASE_FV_V2,
                   help=f"Gate 8f secondary shadow threshold (default: {DEFAULT_SHADOW_RELAXED_MAX_BASE_FV_V2}). "
                        f"Tests whether signals in [strict, v2) would have won. "
                        f"Use with outcomes JSONL to bracket optimal FV saturation cutoff.")
    p.add_argument("--shadow-relaxed-pace-buffer", type=float, default=DEFAULT_SHADOW_RELAXED_PACE_BUFFER,
                   help=f"Shadow relaxed runs-pace buffer (line - buffer; default: {DEFAULT_SHADOW_RELAXED_PACE_BUFFER}).")
    p.add_argument("--shadow-relaxed-min-inning-offset", type=int, default=DEFAULT_SHADOW_RELAXED_MIN_INNING_OFFSET,
                   help=f"Gate 1 shadow: relax min_inning by this many innings (default: {DEFAULT_SHADOW_RELAXED_MIN_INNING_OFFSET}).")
    p.add_argument("--shadow-relaxed-min-inning-high-line-offset", type=int, default=DEFAULT_SHADOW_RELAXED_MIN_INNING_HIGH_LINE_OFFSET,
                   help=f"Gate 1 shadow: relax min_inning_high_line by this many innings (default: {DEFAULT_SHADOW_RELAXED_MIN_INNING_HIGH_LINE_OFFSET}).")
    p.add_argument("--prob-calibration-mode", choices=["off", "shadow", "enforce"], default=DEFAULT_PROB_CALIBRATION_MODE,
                   help=f"Probability calibration mode for fair_value (default: {DEFAULT_PROB_CALIBRATION_MODE}).")
    p.add_argument("--prob-calibration-enforce-min-raw", type=float,
                   default=DEFAULT_PROB_CALIBRATION_ENFORCE_MIN_RAW,
                   help=(
                       "Band-gated enforce: when --prob-calibration-mode=enforce, "
                       "only overwrite raw FV when raw_prob >= this threshold. "
                       "Below threshold, raw is kept (calibrator still scored "
                       "for observability). 0.0 disables the gate (enforce "
                       "across whole range). 2026-05-19 audit chose 0.90 to "
                       "capture the [0.95,1.00) +28pp overconfidence band "
                       f"without amputating mid-band bets (default: {DEFAULT_PROB_CALIBRATION_ENFORCE_MIN_RAW})."
                   ))
    # Active #8 prep (2026-05-17): Stage-1 Alt A shadow-empirical
    # override. Mirrors --stage1-shadow-empirical-override on the
    # live_engine CLI so paper mode can opt into per-tick Alt A
    # logging too. SignalEngine reads `trade_args.stage1_shadow_empirical_mode`;
    # the live CLI bridges its `--stage1-shadow-empirical-override` to
    # the same attribute, so live + paper now share one runtime
    # contract. Default `off`: no math, no logging change.
    p.add_argument("--stage1-shadow-empirical-mode",
                   dest="stage1_shadow_empirical_mode",
                   choices=["off", "shadow"], default="off",
                   help=(
                       "Stage-1 Alt A (empirical-when-available) runtime "
                       "mode. shadow = compute fair_value_alt_empirical "
                       "alongside production fair_value on every candidate "
                       "and log both; NO decision change. The offline "
                       "build_stage1_shadow_override_report.py consumes "
                       "the logged alt FVs to surface cumulative shadow "
                       "improvement. (default: off)"
                   ))
    p.add_argument("--prob-calibration-path", type=Path, default=DEFAULT_PROB_CALIBRATION_PATH,
                   help=f"Path to probability calibration artifact JSON (default: {DEFAULT_PROB_CALIBRATION_PATH}).")
    # Phase A5 (2026-05-19): UNDER candidate emission. paper-only at
    # first; the eventual paper-mode flip is gated by B4 in the
    # roadmap (60-session UNDER validation milestone).
    p.add_argument(
        "--under-emission-mode",
        type=str,
        choices=UNDER_EMISSION_MODES,
        default=DEFAULT_UNDER_EMISSION_MODE,
        help=(
            "UNDER candidate emission. `off` (default): no UNDER "
            "candidates emitted by the live engine. `shadow`: alongside "
            "every OVER candidate that reaches the FV phase, the engine "
            "emits a sibling UNDER candidate row (decision_reason "
            "tagged `shadow_under` when UNDER gates pass). NO UNDER "
            "bets are placed in either mode -- this is observability "
            "for Phase A5 of the bidirectional pivot; the eventual "
            "UNDER paper-bet flip is a separate ship gated by B4 "
            "validation."
        ),
    )
    p.add_argument(
        "--prob-calibration-under-path",
        type=Path,
        default=DEFAULT_PROB_CALIBRATION_UNDER_PATH,
        help=(
            f"Path to UNDER probability calibration artifact JSON "
            f"(default: {DEFAULT_PROB_CALIBRATION_UNDER_PATH}). Loaded "
            "when --under-emission-mode is `shadow`; otherwise unused."
        ),
    )
    p.add_argument("--shadow-no-score-drift-enabled", dest="shadow_no_score_drift_enabled",
                   action="store_true", default=DEFAULT_SHADOW_NO_SCORE_DRIFT_ENABLED,
                   help="Enable shadow candidate logging for no-score drift-low state-value setups.")
    p.add_argument("--no-shadow-no-score-drift", dest="shadow_no_score_drift_enabled",
                   action="store_false",
                   help="Disable shadow candidate logging for no-score drift-low setups.")
    p.add_argument("--shadow-no-score-drift-min-inning", type=int,
                   default=DEFAULT_SHADOW_NO_SCORE_DRIFT_MIN_INNING,
                   help=f"Minimum inning for no-score drift shadow rows (default: {DEFAULT_SHADOW_NO_SCORE_DRIFT_MIN_INNING}).")
    p.add_argument("--shadow-no-score-drift-max-inning", type=int,
                   default=DEFAULT_SHADOW_NO_SCORE_DRIFT_MAX_INNING,
                   help=f"Maximum inning for no-score drift shadow rows (default: {DEFAULT_SHADOW_NO_SCORE_DRIFT_MAX_INNING}).")
    p.add_argument("--shadow-no-score-drift-min-segment-age-secs", type=float,
                   default=DEFAULT_SHADOW_NO_SCORE_DRIFT_MIN_SEGMENT_AGE_SECS,
                   help=f"Minimum same-score segment age in seconds (default: {DEFAULT_SHADOW_NO_SCORE_DRIFT_MIN_SEGMENT_AGE_SECS}).")
    p.add_argument("--shadow-no-score-drift-min-segment-ticks", type=int,
                   default=DEFAULT_SHADOW_NO_SCORE_DRIFT_MIN_SEGMENT_TICKS,
                   help=f"Minimum same-score segment ticks (default: {DEFAULT_SHADOW_NO_SCORE_DRIFT_MIN_SEGMENT_TICKS}).")
    p.add_argument("--shadow-no-score-drift-min-drawdown", type=float,
                   default=DEFAULT_SHADOW_NO_SCORE_DRIFT_MIN_DRAWDOWN,
                   help=f"Minimum ask drawdown from same-score segment high (default: {DEFAULT_SHADOW_NO_SCORE_DRIFT_MIN_DRAWDOWN}).")
    p.add_argument("--shadow-no-score-drift-min-ask", type=float,
                   default=DEFAULT_SHADOW_NO_SCORE_DRIFT_MIN_ASK,
                   help=f"Minimum ask for no-score drift shadow rows (default: {DEFAULT_SHADOW_NO_SCORE_DRIFT_MIN_ASK}).")
    p.add_argument("--shadow-no-score-drift-max-ask", type=float,
                   default=DEFAULT_SHADOW_NO_SCORE_DRIFT_MAX_ASK,
                   help=f"Maximum ask for no-score drift shadow rows (default: {DEFAULT_SHADOW_NO_SCORE_DRIFT_MAX_ASK}).")
    p.add_argument("--shadow-no-score-drift-max-spread", type=float,
                   default=DEFAULT_SHADOW_NO_SCORE_DRIFT_MAX_SPREAD,
                   help=f"Maximum spread for no-score drift shadow rows (default: {DEFAULT_SHADOW_NO_SCORE_DRIFT_MAX_SPREAD}).")
    p.add_argument("--shadow-no-score-drift-min-po-edge", type=float,
                   default=DEFAULT_SHADOW_NO_SCORE_DRIFT_MIN_PO_EDGE,
                   help=f"Minimum Poisson/current-state edge for no-score drift shadow rows (default: {DEFAULT_SHADOW_NO_SCORE_DRIFT_MIN_PO_EDGE}).")
    p.add_argument("--shadow-no-score-drift-min-emp-edge", type=float,
                   default=DEFAULT_SHADOW_NO_SCORE_DRIFT_MIN_EMP_EDGE,
                   help=f"Minimum empirical/current-state edge for no-score drift shadow rows (default: {DEFAULT_SHADOW_NO_SCORE_DRIFT_MIN_EMP_EDGE}).")
    p.add_argument("--paper-root", type=Path, default=DEFAULT_PAPER_ROOT,
                   help=f"Root directory for paper trading output (default: {DEFAULT_PAPER_ROOT}).")
    # Book capture args (limit order calibration data)
    p.add_argument("--capture-duration", type=float, default=DEFAULT_CAPTURE_DURATION,
                   help=f"Seconds to capture book snapshots after each signal (default: {DEFAULT_CAPTURE_DURATION}).")
    p.add_argument("--capture-interval", type=float, default=DEFAULT_CAPTURE_INTERVAL,
                   help=f"Seconds between book capture snapshots (default: {DEFAULT_CAPTURE_INTERVAL}).")
    p.add_argument("--capture-depth", type=int, default=DEFAULT_CAPTURE_DEPTH,
                   help=f"Top-N bid/ask levels to capture per snapshot (default: {DEFAULT_CAPTURE_DEPTH}).")
    p.add_argument("-h", "--help", action="store_true")

    trade_args, remaining = p.parse_known_args(argv)

    if trade_args.help:
        p.print_help()
        sys.exit(0)

    if trade_args.min_current_total_relax_floor < 0:
        p.error("--min-current-total-relax-floor must be >= 0")
    if trade_args.min_current_total_relax_inning < 1:
        p.error("--min-current-total-relax-inning must be >= 1")
    if not (0.0 <= trade_args.min_current_total_relax_ask_min <= 1.0):
        p.error("--min-current-total-relax-ask-min must be between 0 and 1")
    if trade_args.min_current_total_relax_max_lead < 0:
        p.error("--min-current-total-relax-max-lead must be >= 0")
    if trade_args.min_current_total_relax_max_runs_needed < 0:
        p.error("--min-current-total-relax-max-runs-needed must be >= 0")
    if trade_args.shadow_relaxed_blowout_cond_min_inning < 1:
        p.error("--shadow-relaxed-blowout-cond-min-inning must be >= 1")
    if not (0.0 <= trade_args.shadow_relaxed_blowout_cond_max_ask <= 1.0):
        p.error("--shadow-relaxed-blowout-cond-max-ask must be between 0 and 1")
    if trade_args.blowout_relax_max_inning < 1:
        p.error("--blowout-relax-max-inning must be >= 1")
    if not (0.0 <= trade_args.blowout_relax_min_ask <= 1.0):
        p.error("--blowout-relax-min-ask must be between 0 and 1")
    if trade_args.blowout_relax_max_runs_needed < 0:
        p.error("--blowout-relax-max-runs-needed must be >= 0")
    if trade_args.blowout_relax_near_line_runs_needed < 0:
        p.error("--blowout-relax-near-line-runs-needed must be >= 0")
    if not (0.0 <= trade_args.blowout_relax_near_line_min_ask <= 1.0):
        p.error("--blowout-relax-near-line-min-ask must be between 0 and 1")
    if not (0.0 <= trade_args.gate_relax_ab_fraction <= 1.0):
        p.error("--gate-relax-ab-fraction must be between 0 and 1")
    if not (0.0 <= trade_args.ask_edge_ramp_start <= 1.0):
        p.error("--ask-edge-ramp-start must be between 0 and 1")
    if not (0.0 <= trade_args.ask_edge_ramp_end <= 1.0):
        p.error("--ask-edge-ramp-end must be between 0 and 1")
    if trade_args.ask_edge_ramp_end < trade_args.ask_edge_ramp_start:
        p.error("--ask-edge-ramp-end must be >= --ask-edge-ramp-start")
    if trade_args.ask_edge_ramp_max_boost < 0:
        p.error("--ask-edge-ramp-max-boost must be >= 0")
    if trade_args.shadow_no_score_drift_min_inning < 1:
        p.error("--shadow-no-score-drift-min-inning must be >= 1")
    if trade_args.shadow_no_score_drift_max_inning < trade_args.shadow_no_score_drift_min_inning:
        p.error("--shadow-no-score-drift-max-inning must be >= min inning")
    if trade_args.shadow_no_score_drift_min_segment_age_secs < 0:
        p.error("--shadow-no-score-drift-min-segment-age-secs must be >= 0")
    if trade_args.shadow_no_score_drift_min_segment_ticks < 1:
        p.error("--shadow-no-score-drift-min-segment-ticks must be >= 1")
    if trade_args.shadow_no_score_drift_min_drawdown < 0:
        p.error("--shadow-no-score-drift-min-drawdown must be >= 0")
    if not (0.0 <= trade_args.shadow_no_score_drift_min_ask <= 1.0):
        p.error("--shadow-no-score-drift-min-ask must be between 0 and 1")
    if not (0.0 <= trade_args.shadow_no_score_drift_max_ask <= 1.0):
        p.error("--shadow-no-score-drift-max-ask must be between 0 and 1")
    if trade_args.shadow_no_score_drift_max_ask < trade_args.shadow_no_score_drift_min_ask:
        p.error("--shadow-no-score-drift-max-ask must be >= min ask")
    if trade_args.shadow_no_score_drift_max_spread < 0:
        p.error("--shadow-no-score-drift-max-spread must be >= 0")
    if trade_args.shadow_no_score_drift_min_po_edge < 0:
        p.error("--shadow-no-score-drift-min-po-edge must be >= 0")
    if trade_args.shadow_no_score_drift_min_emp_edge < 0:
        p.error("--shadow-no-score-drift-min-emp-edge must be >= 0")

    import sys as _sys
    old_argv = _sys.argv
    _sys.argv = [old_argv[0]] + remaining
    try:
        monitor_args = monitor_parse_args()
    finally:
        _sys.argv = old_argv

    return trade_args, monitor_args


__all__ = [
    name
    for name in globals()
    if name.startswith('DEFAULT_') or name in {'CONFIRM_RETAIN_FRACTION', 'INACTIVE_INNING_STATES', 'parse_trade_args'}
]

