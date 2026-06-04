"""live_engine_cli.py -- CLI argument parsing + default constants.

Extracted from `live_engine.py` (2026-05-12) to keep the engine file under
the LLM-friendly 1200-line threshold. Owns:
  - All DEFAULT_* constants used as argparse defaults or by the engine init
  - parse_live_args() -- the live-trading CLI (delegates to signal_engine's
    parser for trade and monitor args)

`live_engine.py` re-exports every name in __all__ so callers importing
constants directly from `live_engine` (tests, downstream tools) keep
working without churn.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Tuple

from signal_engine import parse_trade_args  # noqa: E402

from live_engine_overrides import (  # noqa: E402
    DEFAULT_OVERRIDES_PATH as LIVE_ENGINE_OVERRIDES_PATH,
    apply_overrides as _apply_live_engine_overrides,
    load_overrides as _load_live_engine_overrides,
    passed_flags as _passed_argv_flags,
)


PROJECT_DIR = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# Defaults (argparse-facing + a few engine-internal constants)
# ---------------------------------------------------------------------------

DEFAULT_SPREAD_FACTOR            = 0.65    # limit price: bid + spread * factor
# 2026-06-03 fill-optimization fix: cap the maximum below-ask gap on
# the limit-buy price. The pre-fix logic (spread_factor=0.65, no gap
# cap) let limits sit 6-10c below ask in wide-spread regimes, where
# the 34-day historical fill rate was ~50%. Fills above 1.5c below
# ask were ~80-94%. Default 0.02 (= 2c) lands the bot in the high-
# fill zone while preserving some below-ask savings. Audit data:
# moving from current (mean gap ~1.9c, 80% fill) to a 2c cap is
# estimated at +$4-5/day at $20 stake on the 28-cancel sample.
DEFAULT_MAX_LIMIT_GAP_BELOW_ASK  = 0.02    # cap: limit >= ask - 2c
DEFAULT_ORDER_TIMEOUT_SECS       = 10800.0 # safety-net cancel after 3 hours
DEFAULT_MAX_OPEN_ORDERS          = 5       # refuse new bets when this many orders live
DEFAULT_MIN_ORDER_SIZE           = 5.0     # Polymarket minimum order size in USDC
DEFAULT_ENV_FILE                 = PROJECT_DIR / ".env"
DEFAULT_EV_POLICY_REPORT_PATH    = PROJECT_DIR / "data" / "analysis_output" / "ev_policy" / "ev_policy_report.json"
DEFAULT_EV_WIN_MODEL_PATH        = PROJECT_DIR / "data" / "analysis_output" / "ev_policy" / "ev_signal_win_if_filled_model.json"
DEFAULT_EV_FILL_MODEL_PATH       = PROJECT_DIR / "data" / "analysis_output" / "ev_policy" / "ev_execution_fill_runtime_model.json"
DEFAULT_LIVE_STAKE               = 25.0    # dollars per bet when stake_mode=flat
DEFAULT_DAILY_BUDGET             = 125.0   # max total spend per session; resets each run
DEFAULT_PER_GAME_BUDGET_FRACTION = 0.40    # max same-game exposure as fraction of daily budget
# (2026-05-17) Bumped 0.35 -> 0.40 to match the operator's actual
# runtime setting and align with the Phase C C2 inventory cap
# (max_inventory_per_game = 50 shares ~= $50 at typical ask, vs
# $100 daily budget = 50% ceiling; 40% leaves clean headroom).

# Correlated-line exposure cap (Active #6, shipped 2026-05-12). Over 7.5 and
# Over 8.5 on the same game are highly correlated outcomes -- placing both
# effectively doubles exposure on one trade idea. See
# `LiveTradingEngine._evaluate_correlated_line_cap`.
DEFAULT_MAX_CORRELATED_OVER_LINES_PER_GAME = 2
DEFAULT_MIN_CORRELATED_LINE_GAP  = 1.5     # >= this many runs between placed
                                           # over lines on the same game.
                                           # 1.5 blocks O7.5+O8.5 but allows
                                           # O7.5+O9.5.

DEFAULT_STAKE_MODE               = "kelly"
DEFAULT_KELLY_FRACTION           = 0.25    # fraction of full Kelly (quarter-Kelly)
DEFAULT_KELLY_MAX_BET_FRACTION   = 0.33    # cap: no single bet > this fraction of daily_budget
DEFAULT_KELLY_MAX_EDGE           = 0.25    # cap edge-at-limit used by Kelly sizing
DEFAULT_KELLY_FLOOR_TO_MIN       = False   # when False, sub-min Kelly signals are skipped

# Calibrated-edge stake scaling (Active #6 part 2, shipped 2026-05-12).
# Multiplies the base stake by a value in [min, max] derived from the
# *calibrated* edge (fair_value_calibrated - decision_ask). Calibration
# shipped 2026-05-11 with real Platt/isotonic curves, so this multiplier
# is a meaningful signal -- raw FV is overconfident, calibrated FV is the
# more honest probability and its edge is the right input for sizing.
#
# Three modes:
#   off     -- no multiplier computed, stake unchanged
#   shadow  -- multiplier computed and logged on the bet record; stake NOT changed
#   enforce -- multiplier computed AND applied to base stake
#
# Default starts in shadow so the bot's behavior doesn't change today; the
# multiplier just appears on bet records for the operator to audit before
# flipping to enforce.
DEFAULT_CALIBRATED_STAKE_SCALE_MODE = "shadow"
DEFAULT_CALIBRATED_STAKE_MIN_MULTIPLIER = 0.5
DEFAULT_CALIBRATED_STAKE_MAX_MULTIPLIER = 1.5
# At calibrated_edge >= ramp_top_edge the multiplier hits MAX. Default 0.15
# spans the typical edge range we trade (gates require edge >= 0.10/0.15
# for high/low lines, so 0.15 is roughly the upper end of "real" edge).
DEFAULT_CALIBRATED_STAKE_RAMP_TOP_EDGE = 0.15

DEFAULT_SESSION_SAVE_MIN_INTERVAL_SECS = 2.0  # coalesce burst writes to session JSON

# --wait-for-clob defaults
DEFAULT_WAIT_FOR_CLOB_TIMEOUT_SECS = 1800.0  # 30 minutes max wait
DEFAULT_WAIT_FOR_CLOB_POLL_SECS    = 15.0    # poll every 15s

DEFAULT_STARTUP_REFRESH_ENABLED        = True
DEFAULT_STARTUP_WEATHER_PROVIDER       = "open-meteo"
DEFAULT_STARTUP_WEATHER_TIMEOUT_SECS   = 8.0

# FV decay / cancellation params (argparse defaults; also referenced by
# live_order_lifecycle when applying the gate).
FV_CANCEL_MIN_EDGE                 = 0.03
DEFAULT_FV_DECAY_MIN_AGE_SECS      = 90.0
DEFAULT_FV_DECAY_MIN_ASK_DROP      = 0.03
DEFAULT_ASK_REVERSAL_DROP          = 0.08
DEFAULT_ASK_REVERSAL_WINDOW        = 5.0
DEFAULT_ASK_REVERSAL_WINDOW_BUFFER = 10.0

# Wallet-aware paper fallback (shipped 2026-05-13). When the CLOB rejects
# a real-money order with "not enough balance / allowance", the engine
# routes that bet (and subsequent bets within the cooldown window) to a
# synthesized paper-fallback path so signal/outcome data keeps flowing
# without retry storms eating CLOB quota. Cooldown is bounded so real
# money resumes automatically as soon as the wallet frees up (e.g. when
# positions settle).
DEFAULT_WALLET_EXHAUSTED_COOLDOWN_SECS = 300.0  # 5 min


__all__ = [
    "PROJECT_DIR",
    "DEFAULT_SPREAD_FACTOR",
    "DEFAULT_MAX_LIMIT_GAP_BELOW_ASK",
    "DEFAULT_ORDER_TIMEOUT_SECS",
    "DEFAULT_MAX_OPEN_ORDERS",
    "DEFAULT_MIN_ORDER_SIZE",
    "DEFAULT_ENV_FILE",
    "DEFAULT_EV_POLICY_REPORT_PATH",
    "DEFAULT_EV_WIN_MODEL_PATH",
    "DEFAULT_EV_FILL_MODEL_PATH",
    "DEFAULT_LIVE_STAKE",
    "DEFAULT_DAILY_BUDGET",
    "DEFAULT_PER_GAME_BUDGET_FRACTION",
    "DEFAULT_MAX_CORRELATED_OVER_LINES_PER_GAME",
    "DEFAULT_MIN_CORRELATED_LINE_GAP",
    "DEFAULT_STAKE_MODE",
    "DEFAULT_KELLY_FRACTION",
    "DEFAULT_KELLY_MAX_BET_FRACTION",
    "DEFAULT_KELLY_MAX_EDGE",
    "DEFAULT_KELLY_FLOOR_TO_MIN",
    "DEFAULT_CALIBRATED_STAKE_SCALE_MODE",
    "DEFAULT_CALIBRATED_STAKE_MIN_MULTIPLIER",
    "DEFAULT_CALIBRATED_STAKE_MAX_MULTIPLIER",
    "DEFAULT_CALIBRATED_STAKE_RAMP_TOP_EDGE",
    "DEFAULT_SESSION_SAVE_MIN_INTERVAL_SECS",
    "DEFAULT_WAIT_FOR_CLOB_TIMEOUT_SECS",
    "DEFAULT_WAIT_FOR_CLOB_POLL_SECS",
    "DEFAULT_STARTUP_REFRESH_ENABLED",
    "DEFAULT_STARTUP_WEATHER_PROVIDER",
    "DEFAULT_STARTUP_WEATHER_TIMEOUT_SECS",
    "FV_CANCEL_MIN_EDGE",
    "DEFAULT_FV_DECAY_MIN_AGE_SECS",
    "DEFAULT_FV_DECAY_MIN_ASK_DROP",
    "DEFAULT_ASK_REVERSAL_DROP",
    "DEFAULT_ASK_REVERSAL_WINDOW",
    "DEFAULT_ASK_REVERSAL_WINDOW_BUFFER",
    "DEFAULT_WALLET_EXHAUSTED_COOLDOWN_SECS",
    "parse_live_args",
]


def parse_live_args(argv=None) -> Tuple[argparse.Namespace, argparse.Namespace, argparse.Namespace]:
    """Parse live-trading-specific args, then delegate to signal_engine's parser.

    Returns (live_args, trade_args, monitor_args).
    """
    p = argparse.ArgumentParser(
        description="Live trading engine for MLB Polymarket O/U markets.",
        add_help=False,
        # Disable abbreviation matching: without this, --stake is treated as an
        # abbreviation of --stake-mode (prefix match), causing argparse to assign
        # the --stake value to stake_mode and fail with "invalid choice".
        allow_abbrev=False,
    )
    p.add_argument("--dry-run", action="store_true", default=False,
                   help="Log all orders but do NOT post to Polymarket CLOB.")
    p.add_argument("--env-file", type=Path, default=DEFAULT_ENV_FILE,
                   help=f"Path to .env file with POLY_PRIVATE_KEY (default: {DEFAULT_ENV_FILE}).")
    p.add_argument("--use-deposit-wallet", action="store_true", default=False,
                   help="Opt into the Polymarket deposit-wallet (ERC-1271, sig_type=3) "
                        "order signing path. Requires --deposit-wallet or POLY_DEPOSIT_WALLET "
                        "and a deployed/funded deposit wallet on Polygon mainnet. "
                        "Without this flag the legacy proxy (sig_type=2) path is used.")
    p.add_argument("--deposit-wallet", type=str, default=None,
                   help="Deposit-wallet smart-account address (0x...) used when "
                        "--use-deposit-wallet is on. If omitted, POLY_DEPOSIT_WALLET "
                        "is read from the .env file.")
    p.add_argument("--spread-factor", type=float, default=DEFAULT_SPREAD_FACTOR,
                   help=f"Limit price position in spread: bid + spread*factor (default: {DEFAULT_SPREAD_FACTOR})")
    p.add_argument(
        "--max-limit-gap-below-ask", type=float,
        default=DEFAULT_MAX_LIMIT_GAP_BELOW_ASK,
        help=(
            "2026-06-03 fill-optimization: cap the maximum below-ask "
            "gap on limit-buy prices. The bot's natural limit "
            "(bid + spread*spread_factor) can fall many cents below "
            "ask in wide-spread regimes; orders >1.5c below ask have "
            "historically had ~50-80% fill rate vs ~94% for at-ask. "
            "This floor forces limit >= ask - max_gap, dramatically "
            "improving fill rate at the cost of some below-ask "
            f"savings. (default: {DEFAULT_MAX_LIMIT_GAP_BELOW_ASK})"
        ),
    )
    p.add_argument("--order-timeout-secs", type=float, default=DEFAULT_ORDER_TIMEOUT_SECS,
                   help=f"Safety-net: cancel orders older than this many seconds "
                        f"(default: {DEFAULT_ORDER_TIMEOUT_SECS}s = 3 hours). "
                        f"FV decay (--fv-cancel-min-edge) is the primary cancellation path.")
    p.add_argument("--fv-cancel-min-edge", type=float, default=FV_CANCEL_MIN_EDGE,
                   help=f"Cancel open order when (current_fv - limit_price) drops below "
                        f"this value. (default: {FV_CANCEL_MIN_EDGE})")
    p.add_argument("--fv-decay-min-age-secs", type=float, default=DEFAULT_FV_DECAY_MIN_AGE_SECS,
                   help=f"Minimum order age before FV-decay cancellation is allowed. "
                        f"(default: {DEFAULT_FV_DECAY_MIN_AGE_SECS}s)")
    p.add_argument("--fv-decay-min-ask-drop", type=float, default=DEFAULT_FV_DECAY_MIN_ASK_DROP,
                   help=f"Require at least this ask drop before FV-decay cancellation. "
                        f"(default: {DEFAULT_FV_DECAY_MIN_ASK_DROP})")
    p.add_argument("--max-open-orders", type=int, default=DEFAULT_MAX_OPEN_ORDERS,
                   help=f"Refuse new bets when this many orders are unresolved. "
                        f"(default: {DEFAULT_MAX_OPEN_ORDERS})")
    p.add_argument("--session-save-min-interval-secs", type=float, default=DEFAULT_SESSION_SAVE_MIN_INTERVAL_SECS,
                   help=f"Minimum time between session JSON rewrites; burst updates are coalesced. "
                        f"(default: {DEFAULT_SESSION_SAVE_MIN_INTERVAL_SECS}s)")
    p.add_argument("--ask-reversal-drop", type=float, default=DEFAULT_ASK_REVERSAL_DROP,
                   help=f"Cancel within ask-reversal window if ask drops >= this. "
                        f"(default: {DEFAULT_ASK_REVERSAL_DROP})")
    p.add_argument("--ask-reversal-window", type=float, default=DEFAULT_ASK_REVERSAL_WINDOW,
                   help=f"Seconds after placement to check for ask reversal. "
                        f"(default: {DEFAULT_ASK_REVERSAL_WINDOW})")
    p.add_argument("--wallet-exhausted-cooldown-secs", type=float,
                   default=DEFAULT_WALLET_EXHAUSTED_COOLDOWN_SECS,
                   help=f"After CLOB rejects an order with insufficient balance, "
                        f"route subsequent bets to a paper-fallback (synthesized "
                        f"fill at limit price, tracked through settlement) for "
                        f"this many seconds. Real-money attempts resume "
                        f"automatically after the cooldown elapses. Set to 0 to "
                        f"only fallback on the rejected bet itself with no "
                        f"forward cooldown. "
                        f"(default: {DEFAULT_WALLET_EXHAUSTED_COOLDOWN_SECS}s)")
    p.add_argument("--min-order-size", type=float, default=DEFAULT_MIN_ORDER_SIZE,
                   help=f"Polymarket minimum order size in USDC. (default: {DEFAULT_MIN_ORDER_SIZE})")
    p.add_argument("--daily-budget", type=float, default=DEFAULT_DAILY_BUDGET,
                   help=f"Max total USDC deployed per session. (default: {DEFAULT_DAILY_BUDGET})")
    p.add_argument("--per-game-budget-fraction", type=float, default=DEFAULT_PER_GAME_BUDGET_FRACTION,
                   help=f"Max same-game exposure as fraction of daily budget. "
                        f"(default: {DEFAULT_PER_GAME_BUDGET_FRACTION})")
    p.add_argument("--max-correlated-over-lines-per-game", type=int,
                   default=DEFAULT_MAX_CORRELATED_OVER_LINES_PER_GAME,
                   help=(
                       "Max number of over-side bets on the same game. "
                       "Highly correlated outcomes -- placing N of them is "
                       "effectively N-times exposure on one trade idea. "
                       "0 disables. "
                       f"(default: {DEFAULT_MAX_CORRELATED_OVER_LINES_PER_GAME})"
                   ))
    p.add_argument("--min-correlated-line-gap", type=float,
                   default=DEFAULT_MIN_CORRELATED_LINE_GAP,
                   help=(
                       "Minimum runs between placed over lines on the same "
                       "game. 1.5 blocks O7.5+O8.5 (gap=1.0) but allows "
                       "O7.5+O9.5 (gap=2.0). 0 disables. "
                       f"(default: {DEFAULT_MIN_CORRELATED_LINE_GAP})"
                   ))
    p.add_argument("--stake-mode", choices=["flat", "kelly"], default=DEFAULT_STAKE_MODE,
                   help=f"Bet-sizing mode. (default: {DEFAULT_STAKE_MODE})")
    p.add_argument("--kelly-fraction", type=float, default=DEFAULT_KELLY_FRACTION,
                   help=f"Fraction of full Kelly to apply. (default: {DEFAULT_KELLY_FRACTION})")
    p.add_argument("--kelly-max-bet-fraction", type=float, default=DEFAULT_KELLY_MAX_BET_FRACTION,
                   help=f"Cap: max single bet as fraction of daily_budget. "
                        f"(default: {DEFAULT_KELLY_MAX_BET_FRACTION})")
    p.add_argument("--kelly-max-edge", type=float, default=DEFAULT_KELLY_MAX_EDGE,
                   help=f"Cap edge used by Kelly sizing. (default: {DEFAULT_KELLY_MAX_EDGE})")
    p.add_argument("--kelly-floor-to-min", action="store_true", default=DEFAULT_KELLY_FLOOR_TO_MIN,
                   help="Opt into old behavior that floors any positive Kelly stake to min_order_size.")
    p.add_argument("--calibrated-stake-scale-mode",
                   choices=["off", "shadow", "enforce"],
                   default=DEFAULT_CALIBRATED_STAKE_SCALE_MODE,
                   help=(
                       "Scale per-bet stake by a multiplier derived from the "
                       "*calibrated* edge (fair_value_calibrated - decision_ask). "
                       "off = no scaling. shadow = compute + record on the bet "
                       "but don't change stake. enforce = apply the multiplier "
                       "to the base stake. "
                       f"(default: {DEFAULT_CALIBRATED_STAKE_SCALE_MODE})"
                   ))
    # Phase C C1+C2+C3+C4 shadow (2026-05-17). Two-sided quote engine
    # entry point. Defaults to OFF -- operator must explicitly opt in.
    # `shadow` writes per-tick QuoteDecision rows to
    # data/{live,paper}_trading/quote_engine_shadow/<date>_quotes.jsonl
    # but never places an order. Live ("act") modes are deferred to
    # Phase C v2 / Phase D after the B4 paper-validation gate clears.
    p.add_argument("--quote-engine-mode",
                   choices=["off", "shadow"],
                   default="off",
                   help=(
                       "Two-sided quote engine mode. off = disabled "
                       "(default; existing behavior). shadow = compute "
                       "would-have bid + ask + hedge per tick and log "
                       "to quote_engine_shadow/<date>_quotes.jsonl; "
                       "NO order is placed. (default: off)"
                   ))
    # Active #8 prep (2026-05-17). Stage-1 shadow empirical override
    # entry point. Defaults to OFF; operator opts in explicitly. The
    # offline shadow-override report (build_stage1_shadow_override_report.py)
    # already produces Alt A counterfactuals from settled bets (~24/week);
    # this runtime hook adds per-candidate shadow logging so the
    # eventual ENFORCE flip has 200x+ more evidence to weigh. shadow
    # mode: compute fair_value_alt_empirical alongside production
    # fair_value when the cell has an empirical estimate, log both on
    # the candidate row, NO effect on decisions / orders.
    p.add_argument("--stage1-shadow-empirical-override",
                   choices=["off", "shadow"],
                   default="off",
                   help=(
                       "Stage-1 Alt A (empirical-when-available) "
                       "runtime mode. off = no alt computation "
                       "(default; existing behavior). shadow = compute "
                       "fair_value_alt_empirical alongside production "
                       "fair_value when cell empirical is available; "
                       "log both on candidate row; NO change to "
                       "production decisions or trades. The offline "
                       "build_stage1_shadow_override_report.py "
                       "consumes the logged alt FVs to surface the "
                       "cumulative shadow improvement. (default: off)"
                   ))
    # Phase A5 (2026-05-19) -> Phase C-paper (2026-05-27) -> live (2026-05-28).
    p.add_argument("--under-mode",
                   choices=["off", "shadow", "paper", "live"],
                   default="off",
                   help=(
                       "UNDER mode. off (default): no UNDER rows. "
                       "shadow: emit sibling UNDER candidate row but "
                       "never place a bet. paper: emit row AND place a "
                       "paper BetRecord(side=under) when all 5 "
                       "symmetric UNDER gates pass -- feeds the B4 "
                       "60-session validation milestone. live: place a "
                       "REAL limit BUY on the under_no token via the "
                       "same CLOB path as OVER (same budget / exposure "
                       "caps / lifecycle). NOTE: the UNDER calibrator is "
                       "flagged unreliable_pre_refit and the asymmetric "
                       "UNDER gate stack is not yet designed -- live is "
                       "an accepted-loss data-gathering posture, not a "
                       "validated edge."
                   ))
    # Backward-compat alias for the old flag name.
    p.add_argument("--under-emission-mode",
                   dest="under_emission_mode_legacy",
                   choices=["off", "shadow"],
                   default=None,
                   help=(
                       "DEPRECATED: legacy alias for --under-mode. "
                       "Accepts only {off, shadow}. Use --under-mode "
                       "for the new `paper` value."
                   ))
    p.add_argument("--calibrated-stake-min-multiplier",
                   type=float,
                   default=DEFAULT_CALIBRATED_STAKE_MIN_MULTIPLIER,
                   help=(
                       "Lower bound for the calibrated-stake multiplier. Fires "
                       "when calibrated edge <= 0. "
                       f"(default: {DEFAULT_CALIBRATED_STAKE_MIN_MULTIPLIER})"
                   ))
    p.add_argument("--calibrated-stake-max-multiplier",
                   type=float,
                   default=DEFAULT_CALIBRATED_STAKE_MAX_MULTIPLIER,
                   help=(
                       "Upper bound for the calibrated-stake multiplier. Fires "
                       "when calibrated edge >= --calibrated-stake-ramp-top-edge. "
                       f"(default: {DEFAULT_CALIBRATED_STAKE_MAX_MULTIPLIER})"
                   ))
    p.add_argument("--calibrated-stake-ramp-top-edge",
                   type=float,
                   default=DEFAULT_CALIBRATED_STAKE_RAMP_TOP_EDGE,
                   help=(
                       "Calibrated edge at which the multiplier reaches its "
                       "maximum. The ramp is linear from edge=0 -> min "
                       "through edge=ramp_top -> max, clamped at both ends. "
                       f"(default: {DEFAULT_CALIBRATED_STAKE_RAMP_TOP_EDGE})"
                   ))
    p.add_argument("--ev-policy-mode", choices=["off", "shadow", "enforce"], default="off",
                   help="EV policy mode: off (disabled), shadow (log only), enforce (gates bets).")
    p.add_argument("--ev-policy-report-path", type=Path, default=DEFAULT_EV_POLICY_REPORT_PATH)
    p.add_argument("--ev-policy-win-model-path", type=Path, default=DEFAULT_EV_WIN_MODEL_PATH)
    p.add_argument("--ev-policy-fill-model-path", type=Path, default=DEFAULT_EV_FILL_MODEL_PATH)
    # (2026-05-17) Default flipped False -> True. The wait-for-clob
    # path is pure operational robustness -- survives scheduled CLOB
    # downtime windows during startup with no impact on trading
    # behavior. The original False default existed for debug
    # iteration speed; that's a niche case worth requiring an
    # explicit --no-wait-for-clob to opt out of.
    p.add_argument("--wait-for-clob",
                   action=argparse.BooleanOptionalAction,
                   default=True,
                   help="Wait for CLOB API to become available before starting the session. "
                        "Useful during scheduled maintenance windows (e.g. deposit wallet rollout). "
                        "Polls every 15s, times out after --wait-for-clob-timeout-secs. "
                        "Pass --no-wait-for-clob to fail-fast at startup if CLOB isn't ready "
                        "(debug / iteration only).")
    p.add_argument("--wait-for-clob-timeout-secs", type=float,
                   default=DEFAULT_WAIT_FOR_CLOB_TIMEOUT_SECS,
                   help=f"Max seconds to wait for CLOB availability with --wait-for-clob. "
                        f"(default: {DEFAULT_WAIT_FOR_CLOB_TIMEOUT_SECS}s = 30 min)")
    p.add_argument("--startup-refresh", dest="startup_refresh", action="store_true",
                   default=DEFAULT_STARTUP_REFRESH_ENABLED,
                   help="Refresh completed-session analysis artifacts before live startup. "
                        "Default: enabled.")
    p.add_argument("--no-startup-refresh", dest="startup_refresh", action="store_false",
                   help="Skip startup analysis refresh for a faster launch.")
    p.add_argument("--startup-refresh-strict", action="store_true", default=False,
                   help="Abort live startup if any startup refresh step fails. "
                        "Default is fail-open with warnings.")
    p.add_argument("--startup-refresh-max-date", type=str, default="",
                   help="Override max session date folded into startup refresh (YYYY-MM-DD). "
                        "Default excludes the active run date.")
    p.add_argument("--startup-refresh-include-run-date", action="store_true", default=False,
                   help="Include the active run date in startup refresh. Use only after that "
                        "session is complete; default excludes in-progress sessions.")
    p.add_argument("--startup-refresh-skip-pitcher-cache", action="store_true", default=False,
                   help="Do not refresh cache/pitcher_cache.json during startup refresh.")
    p.add_argument("--startup-refresh-skip-weather-cache", action="store_true", default=False,
                   help="Do not refresh cache/weather/game_weather_<date>.json during startup refresh.")
    p.add_argument("--startup-weather-provider", choices=["open-meteo", "none"],
                   default=DEFAULT_STARTUP_WEATHER_PROVIDER,
                   help=f"Weather provider used by startup cache refresh. Use 'none' to write "
                        f"metadata-only Weather v2 rows with unknown temp/wind buckets. "
                        f"(default: {DEFAULT_STARTUP_WEATHER_PROVIDER})")
    p.add_argument("--startup-weather-timeout", type=float,
                   default=DEFAULT_STARTUP_WEATHER_TIMEOUT_SECS,
                   help=f"Per-request timeout for startup weather refresh. "
                        f"(default: {DEFAULT_STARTUP_WEATHER_TIMEOUT_SECS}s)")
    p.add_argument("--startup-refresh-skip-daily-reviews", action="store_true", default=False,
                   help="Do not refresh compact daily human-review reports during startup refresh.")
    p.add_argument("--startup-refresh-skip-walk-forward", action="store_true", default=False,
                   help="Do not run score-event/no-score walk-forward refresh during startup.")
    p.add_argument("--startup-refresh-skip-recent-games-scrape", action="store_true", default=False,
                   help="Do not scrape recently-completed MLB games (Stage-3 input) during startup refresh.")
    p.add_argument("--startup-refresh-recent-games-lookback-days", type=int, default=7,
                   help="How many days back to backfill scraped games during startup refresh (default: 7).")
    p.add_argument("--startup-refresh-skip-active-schedule-scrape", action="store_true", default=False,
                   help="Do not refresh today's MLB schedule during startup refresh.")
    p.add_argument("--startup-refresh-skip-stage1-cache", action="store_true", default=False,
                   help="Do not rebuild cache/mlb_ou_cache.json during startup refresh.")
    p.add_argument("--startup-refresh-skip-team-game-log", action="store_true", default=False,
                   help="Do not rebuild Stage-3 team_game_log explicitly during startup refresh.")
    p.add_argument("--startup-refresh-skip-park-hr-factors", action="store_true", default=False,
                   help="Do not rebuild Stage-2 park HR factor cache during startup refresh.")
    p.add_argument("--startup-refresh-skip-preflight-secrets", action="store_true", default=False,
                   help="Skip the .env / POLY_PRIVATE_KEY preflight check during startup refresh.")
    p.add_argument("--startup-refresh-skip-preflight-artifacts", action="store_true", default=False,
                   help="Skip the Stage-1/2/3 cache preflight check during startup refresh.")
    p.add_argument("--startup-refresh-require-poly-private-key", action="store_true", default=False,
                   help="Treat missing POLY_PRIVATE_KEY as a hard preflight failure during startup refresh.")
    p.add_argument("-h", "--help", action="store_true")

    live_args, remaining = p.parse_known_args(argv)

    if live_args.help:
        p.print_help()
        print()
        sys.exit(0)

    if live_args.kelly_max_edge <= 0:
        p.error("--kelly-max-edge must be > 0")
    if live_args.calibrated_stake_min_multiplier < 0:
        p.error("--calibrated-stake-min-multiplier must be >= 0")
    if live_args.calibrated_stake_max_multiplier < live_args.calibrated_stake_min_multiplier:
        p.error("--calibrated-stake-max-multiplier must be >= --calibrated-stake-min-multiplier")
    if live_args.calibrated_stake_ramp_top_edge <= 0:
        p.error("--calibrated-stake-ramp-top-edge must be > 0")
    if live_args.fv_decay_min_ask_drop < 0:
        p.error("--fv-decay-min-ask-drop must be >= 0")
    if live_args.session_save_min_interval_secs < 0:
        p.error("--session-save-min-interval-secs must be >= 0")
    if live_args.startup_refresh_max_date and len(live_args.startup_refresh_max_date) != 10:
        p.error("--startup-refresh-max-date must be YYYY-MM-DD")
    if live_args.startup_weather_timeout <= 0:
        p.error("--startup-weather-timeout must be > 0")

    trade_args, monitor_args = parse_trade_args(remaining)

    # Apply runtime overrides from cache/live_engine_overrides.json.
    # The file is mutated by `promote.py stake-scaling` / `promote.py
    # gate-threshold` (and the auto-daemon when in `act` mode). Explicit
    # CLI flags still win -- we only override args the operator did NOT
    # pass on the command line.
    overrides = _load_live_engine_overrides(LIVE_ENGINE_OVERRIDES_PATH)
    if overrides:
        notes = _apply_live_engine_overrides(
            live_args=live_args,
            trade_args=trade_args,
            overrides=overrides,
            passed=_passed_argv_flags(argv),
        )
        for note in notes:
            print(f"live_engine_overrides: {note}", file=sys.stderr)

    return live_args, trade_args, monitor_args
