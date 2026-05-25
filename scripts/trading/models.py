"""
models.py -- Data model classes for the MLB Polymarket trading pipeline.

All dataclasses used across the signal engine, live engine, and Polymarket
client live here. Pure data -- no business logic, no I/O, no external imports
beyond the standard library.

Import hierarchy:
    models.py
        ^-- signal_engine.py  (uses BetRecord)
        ^-- live_engine.py    (uses BetRecord, LiveBetRecord)
        ^-- polymarket_client.py  (uses OrderResult, OrderStatus, TradeRecord)
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field
from typing import Optional


# ---------------------------------------------------------------------------
# Bet records
# ---------------------------------------------------------------------------

@dataclass
class BetRecord:
    """
    Core record for one betting signal, used for both paper simulation and
    live trading (LiveBetRecord extends this).

    Populated at signal time; settlement fields filled when the game goes Final.
    """
    bet_id: str
    placed_at: str          # ISO UTC
    game_pk: int
    away_abbrev: str
    home_abbrev: str
    line: str               # e.g. "8.5"
    side: str               # "over" (only direction we bet)
    entry_ask: float        # market ask at signal time (used for edge calc)
    fair_value: float           # final adjusted FV (Stage-1 + Stage-2 + Stage-3)
    base_fair_value: float      # raw Stage-1 FV before environment adjustments
    stage2_run_env_delta: float # logit delta from Stage-2 park/weather model
    team_offense_delta: float   # logit delta from Stage-3 TeamOffenseModel
    edge: float             # fair_value - entry_ask
    inferred_runs: int      # runs we think scored to trigger the signal
    inning: int
    inning_state: str
    outs: int
    away_score_before: int
    home_score_before: int
    inferred_away_after: int
    inferred_home_after: int
    stake: float            # notional dollars
    runners_on: int         # bitmask: 1=1st, 2=2nd, 4=3rd
    venue_name: str = ""    # stadium (from ScheduledGame.venue_name, park analytics)
    ltp_at_signal: Optional[float] = None  # last traded price at signal time; gap vs entry_ask measures adversarial selection risk
    config_label: str = "default"  # parallel paper/live config identifier for A/B analysis

    # Inferred Stage-1 audit fields -- non-enforcing research fields.
    inferred_state_base_poisson: Optional[float] = None
    inferred_state_base_empirical: Optional[float] = None
    inferred_state_poisson_minus_empirical: Optional[float] = None
    inferred_state_empirical_edge: Optional[float] = None
    inferred_state_n: Optional[float] = None
    inferred_state_n_samples: Optional[float] = None
    inferred_state_weighted_n: Optional[float] = None
    inferred_state_effective_n: Optional[float] = None
    inferred_state_effective_n_proxy: Optional[float] = None
    inferred_state_stage1_trust_weight: Optional[float] = None
    inferred_state_stage1_support_bucket: str = ""
    inferred_state_exact_cell_support: Optional[bool] = None
    inferred_state_poisson_line_exact: Optional[bool] = None
    inferred_state_empirical_line_exact: Optional[bool] = None
    inferred_state_empirical_sample_support: Optional[float] = None
    inferred_state_empirical_sample_bucket: str = ""
    inferred_state_state_fallback_penalty: Optional[float] = None
    inferred_state_line_fallback_penalty: Optional[float] = None
    inferred_state_fallback_level: Optional[int] = None
    inferred_state_fallback_label: str = ""
    inferred_state_cell_key: str = ""
    inferred_state_line_key_poisson: str = ""
    inferred_state_line_key_empirical: str = ""
    inferred_state_line_fallback_mode: str = ""
    inferred_state_empirical_line_fallback_mode: str = ""
    inferred_state_empirical_line_source_key: str = ""
    inferred_state_empirical_line_source_key_low: str = ""
    inferred_state_empirical_line_source_key_high: str = ""
    inferred_state_used_fallback: Optional[bool] = None
    inferred_state_base_source: str = ""

    # Remaining-offense diagnostics -- non-enforcing research fields.
    home_leading_late: Optional[bool] = None
    batting_team_is_home: Optional[bool] = None
    bottom9_available_if_needed: Optional[bool] = None
    expected_remaining_half_innings: Optional[float] = None
    expected_remaining_pa_bucket: str = ""
    home_skip_bottom9_risk: Optional[float] = None

    # Scoring-path diagnostics -- non-enforcing research/modeling fields.
    scoring_path_available: Optional[bool] = None
    scoring_path_innings_observed: Optional[int] = None
    scoring_path_runs_observed: Optional[int] = None
    scoring_path_inning_runs: str = ""
    scoring_inning_rate: Optional[float] = None
    scoring_half_rate: Optional[float] = None
    burst_share: Optional[float] = None
    scoreless_streak: Optional[int] = None
    recent2_run_share: Optional[float] = None
    weighted_run_inning_norm: Optional[float] = None
    inning_run_slope: Optional[float] = None

    # State-value transition diagnostics -- non-enforcing research fields.
    state_value_strategy: str = ""
    current_state_value_edge: Optional[float] = None
    current_state_value_fv_raw: Optional[float] = None
    current_state_value_empirical_edge: Optional[float] = None
    current_state_value_effective_n_proxy: Optional[float] = None
    current_state_value_stage1_trust_weight: Optional[float] = None
    current_state_value_stage1_support_bucket: str = ""
    current_state_value_exact_cell_support: Optional[bool] = None
    current_state_value_poisson_line_exact: Optional[bool] = None
    current_state_value_empirical_line_exact: Optional[bool] = None
    current_state_value_empirical_sample_support: Optional[float] = None
    current_state_value_empirical_sample_bucket: str = ""
    current_state_value_state_fallback_penalty: Optional[float] = None
    current_state_value_line_fallback_penalty: Optional[float] = None
    shadow_fv_inferred_lift: Optional[float] = None
    shadow_no_event_edge: Optional[float] = None
    shadow_after_event_edge: Optional[float] = None
    shadow_p_score_event_proxy: Optional[float] = None
    shadow_phantom_risk_score: Optional[float] = None
    shadow_phantom_risk_band: str = ""
    shadow_transition_model: str = ""
    shadow_low_ask_high_edge: Optional[bool] = None
    shadow_runs_needed_exact_3p5: Optional[bool] = None
    shadow_current_state_edge_bucket: str = ""
    shadow_phantom_risk_bucket: str = ""
    shadow_current_phantom_combo_bucket: str = ""
    shadow_inning_bucket: str = ""
    shadow_inning_runs_needed_bucket: str = ""
    shadow_bottom9_home_lead_context: str = ""
    shadow_home_skip_bottom9_risk_bucket: str = ""

    # Settlement fields -- populated when game goes Final
    settled: bool = False
    settled_at: str = ""
    final_away: Optional[int] = None
    final_home: Optional[int] = None
    final_total: Optional[int] = None
    won: Optional[bool] = None
    payout: Optional[float] = None   # paper: stake / entry_ask; live: shares paid if won
    profit: Optional[float] = None   # payout - realized cost


# Backward-compatible alias
PaperBet = BetRecord


@dataclass
class LiveBetRecord(BetRecord):
    """
    Extends BetRecord with real CLOB order lifecycle fields.

    The core BetRecord fields are populated identically to paper simulation.
    Additional fields track the Polymarket CLOB order lifecycle: placement,
    fill, cancellation, and ask-reversal monitoring.

    Note:
        entry_ask  -- market ask at signal time (edge calculation basis)
        limit_price -- price we actually posted to the CLOB (always <= entry_ask)
        fill_price  -- execution price if filled (may be better than limit_price)
    """
    # Decision / execution lineage
    decision_ask: Optional[float] = None
    execution_bid: Optional[float] = None
    execution_ask: Optional[float] = None
    posted_limit: Optional[float] = None
    actual_fill_price: Optional[float] = None

    # Order placement
    limit_price: float = 0.0
    order_size_shares: Optional[float] = None  # shares submitted to CLOB for intended USDC stake
    order_id: Optional[str] = None
    order_placed_at: str = ""
    order_status: str = "pending"   # pending|live|filled|cancelled|expired|error
    clob_accept_status: Optional[str] = None  # raw CLOB status returned at placement
    over_token_id: str = ""         # Polymarket token ID for the OVER outcome

    # Placement provenance (shipped 2026-05-13 with wallet-aware paper
    # fallback). "live" = real CLOB order, real money. "paper_fallback" =
    # CLOB rejected with insufficient balance (or wallet is in cooldown);
    # the bet is synthesized as if filled at the limit price and tracked
    # for signal/outcome data without touching the CLOB. Analysis scripts
    # filter on this when computing real-money P&L vs. signal-tracking
    # metrics.
    placement_mode: str = "live"                 # "live" | "paper_fallback"
    paper_fallback_reason: Optional[str] = None  # "wallet_cooldown" | "clob_balance_error"
    # Raw CLOB / SDK error message preserved on the bet record at placement
    # time. Populated when order_status="error" (real CLOB rejection that
    # didn't qualify for paper-fallback) AND when a paper-fallback bet was
    # synthesized from a wallet-balance error (so the underlying SDK message
    # is recoverable from session JSON without log-grep). Closes the
    # observability gap where 62 errored placements on 2026-05-12 had only
    # `order_status="error"` with no reason recorded structurally.
    placement_error: Optional[str] = None

    # Fill details (populated when order_status == "filled")
    fill_price: Optional[float] = None
    fill_size: Optional[float] = None   # matched shares, not USDC notional
    fill_cost: Optional[float] = None   # realized USDC cost = fill_size * fill_price
    filled_at: Optional[str] = None
    filled_shares: Optional[float] = None  # audit alias for fill_size
    fill_cost_usdc: Optional[float] = None # audit alias for fill_cost
    payout_usdc: Optional[float] = None    # settlement payout in USDC terms

    # Stake sizing inputs
    stake_mode: str = "flat"                          # "flat" | "kelly"
    kelly_full_fraction: Optional[float] = None       # effective f* after edge cap
    kelly_fraction_used: Optional[float] = None       # kelly_fraction param (e.g. 0.25)

    # Calibrated-edge stake scaling (Active #6 part 2, shipped 2026-05-12).
    # When --calibrated-stake-scale-mode is shadow or enforce, a multiplier
    # in [min, max] is computed from the calibrated edge (calibrated FV -
    # decision ask). Shadow mode records the multiplier without applying;
    # enforce mode multiplies the base stake by it.
    calibrated_stake_mode: str = "off"                  # "off" | "shadow" | "enforce"
    calibrated_stake_base_stake: Optional[float] = None  # stake before multiplier
    calibrated_stake_multiplier: Optional[float] = None  # what was computed
    calibrated_stake_edge_used: Optional[float] = None   # the calibrated edge feeding the multiplier
    calibrated_stake_applied: Optional[bool] = None      # True only when mode == enforce

    # Ask-reversal tracking (recorded for every order, not just cancelled ones)
    ask_5s: Optional[float] = None           # best ask ~5s after placement
    ask_drop_5s: Optional[float] = None      # entry_ask - ask_5s; positive = fell
    ask_reversal_recorded: bool = False

    # Cancellation
    cancelled_at: Optional[str] = None
    cancel_reason: str = ""       # timeout|game_final|fv_decay|shutdown|ask_reversal
    fv_at_cancel: Optional[float] = None
    ask_at_cancel: Optional[float] = None


# Backward-compatible alias
LiveBet = LiveBetRecord


# ---------------------------------------------------------------------------
# Polymarket CLOB result types
# ---------------------------------------------------------------------------

@dataclass
class OrderResult:
    """Return type for CLOBOrderClient.place_limit_buy()."""
    success: bool
    order_id: Optional[str] = None
    status: Optional[str] = None      # "live" | "matched" | "delayed"
    error: Optional[str] = None
    sign_ms: float = 0.0
    post_ms: float = 0.0
    size_shares: Optional[float] = None
    notional_usdc: Optional[float] = None


@dataclass
class OrderStatus:
    """Return type for CLOBOrderClient.get_order()."""
    order_id: str
    status: str                       # "live"|"filled"|"cancelled"|"expired"|"unknown"
    size_matched: float = 0.0
    price: Optional[float] = None
    error: Optional[str] = None


@dataclass
class TradeRecord:
    """A confirmed trade from the user's trade history (fill recovery fallback)."""
    trade_id: str
    price: float
    size: float
    timestamp_iso: str                # ISO UTC string


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ts_to_iso(raw_ts) -> str:
    """Convert a raw timestamp (int seconds, int ms, or ISO string) to ISO UTC."""
    if raw_ts is None:
        return _dt.datetime.now(_dt.timezone.utc).isoformat().replace("+00:00", "Z")
    if isinstance(raw_ts, str):
        return raw_ts.rstrip("Z").replace("+00:00", "") + "Z"
    try:
        ts_int = int(raw_ts)
        if ts_int > 1_000_000_000_000:
            ts_int //= 1000
        return (
            _dt.datetime.fromtimestamp(ts_int, tz=_dt.timezone.utc)
            .isoformat()
            .replace("+00:00", "Z")
        )
    except Exception:
        return str(raw_ts)
