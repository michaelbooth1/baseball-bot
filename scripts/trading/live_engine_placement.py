"""live_engine_placement.py -- The live `_place_bet` execution path.

Extracted from `live_engine.py` (2026-05-12) to keep the engine file under
the LLM-friendly 1200-line threshold. Owns the entire bet-placement
sequence from fresh-book lookup through CLOB post:

  1. Pull a fresh executable depth snapshot for the token (decision book
     is preserved separately for record-keeping).
  2. Compute the resting limit price (preserving model edge).
  3. Size the stake (flat or Kelly).
  4. Run the optional EV-policy gate.
  5. Apply daily-budget, per-game-budget, correlated-line, and max-open-orders caps.
  6. Build a LiveBetRecord with all decision-time + execution-time fields.
  7. Place the order via CLOBOrderClient (or simulate it in --dry-run).
  8. Append a tagged row to the live ledger and save the session.

Live-engine methods stay thin wrappers; the bulk of the logic lives here.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Dict, Optional

from live_engine_cli import (
    DEFAULT_CALIBRATED_STAKE_MAX_MULTIPLIER,
    DEFAULT_CALIBRATED_STAKE_MIN_MULTIPLIER,
    DEFAULT_CALIBRATED_STAKE_RAMP_TOP_EDGE,
    DEFAULT_CALIBRATED_STAKE_SCALE_MODE,
    DEFAULT_KELLY_FRACTION,
    DEFAULT_KELLY_MAX_BET_FRACTION,
    DEFAULT_KELLY_MAX_EDGE,
    DEFAULT_PER_GAME_BUDGET_FRACTION,
    DEFAULT_STAKE_MODE,
    DEFAULT_WALLET_EXHAUSTED_COOLDOWN_SECS,
)
from live_pricing import (
    calibrated_stake_multiplier,
    resolve_calibrated_edge,
)
from model_families import SCORE_EVENT_TRANSITION
from models import LiveBetRecord, signal_context_fields
from order_status import (
    is_exposure_counted_status as _is_exposure_counted_status,
    normalize_accepted_order_status as _normalize_accepted_order_status,
)
from signal_engine import (
    BetRecord,
    _inning_state_to_half,
    _now_iso,
    _now_ts,
)

if TYPE_CHECKING:
    from live_engine import LiveTradingEngine


LOGGER = logging.getLogger("live_engine")


# ---------------------------------------------------------------------------
# Wallet-aware paper fallback (shipped 2026-05-13)
#
# When the CLOB rejects a real-money order with "not enough balance /
# allowance", we don't want to (a) keep retrying the same signal at the
# CLOB (yesterday's session burned 62 attempts on the same handful of
# signals), or (b) drop the signal entirely (we lose the outcome data).
# Instead, we synthesize a paper-fallback bet: filled at the limit price,
# tracked through the normal settlement path, marked
# `placement_mode="paper_fallback"` for downstream filtering. A
# session-level cooldown skips CLOB attempts for subsequent placements
# until the wallet has had time to free up (e.g. when an existing
# position settles). Real money resumes automatically after the cooldown.
# ---------------------------------------------------------------------------

_WALLET_EXHAUSTED_PATTERNS = ("not enough balance", "not enough allowance")


def _is_balance_error(err_text: Optional[str]) -> bool:
    """True iff the CLOB error text indicates a wallet-balance shortfall."""
    if not err_text:
        return False
    low = err_text.lower()
    return any(p in low for p in _WALLET_EXHAUSTED_PATTERNS)


def _wallet_cooldown_remaining(engine: "LiveTradingEngine") -> float:
    """Seconds remaining on the wallet-exhausted cooldown, or 0 if inactive."""
    until = getattr(engine, "_wallet_exhausted_until", None)
    if until is None:
        return 0.0
    remaining = until - time.monotonic()
    return remaining if remaining > 0 else 0.0


def _trip_wallet_cooldown(
    engine: "LiveTradingEngine",
    *,
    reason: str,
) -> float:
    """Set the wallet-exhausted cooldown and update the audit counters.

    Returns the configured cooldown duration in seconds. If the duration
    is 0 (or negative), the cooldown does not extend forward -- only the
    bet that triggered the rejection is paper-fallback'd, and subsequent
    bets attempt the CLOB normally.
    """
    cooldown_secs = float(
        getattr(
            getattr(engine, "live_args", None),
            "wallet_exhausted_cooldown_secs",
            DEFAULT_WALLET_EXHAUSTED_COOLDOWN_SECS,
        )
    )
    if cooldown_secs > 0:
        engine._wallet_exhausted_until = time.monotonic() + cooldown_secs
    stats = engine._paper_fallback_stats
    stats["wallet_exhausted_events"] = int(stats.get("wallet_exhausted_events", 0)) + 1
    stats["wallet_exhausted_last_at"] = _now_iso()
    stats["last_reason"] = reason
    LOGGER.warning(
        "Wallet exhausted (%s); routing this bet to paper-fallback. "
        "Subsequent placements will skip the CLOB for the next %.0fs "
        "(real money resumes automatically when cooldown elapses).",
        reason, cooldown_secs,
    )
    return cooldown_secs


def _record_paper_fallback_bet(
    engine: "LiveTradingEngine",
    bet: LiveBetRecord,
    *,
    reason: str,
    placement_error: Optional[str] = None,
) -> LiveBetRecord:
    """Mark `bet` as a paper-fallback bet: synthesize a fill at the limit
    price, add it to the engine's bet list (already there from the caller)
    so settlement picks it up at game-final, and persist the lifecycle
    event. The bet is NOT added to `_open_orders` -- it has no real CLOB
    order to track, and the SDK polling path would try to fetch a fake
    order_id and warn.

    `placement_error` (when supplied) records the original SDK error that
    triggered the fallback. We always know `reason` (a coarse bucket like
    "clob_balance_error") but the raw message is what tells you whether
    it was a $0.42-vs-$8 wallet shortfall or a partial-fill follow-up
    rejection. Cheap to preserve; expensive to dig out of logs later.
    """
    bet.placement_mode = "paper_fallback"
    bet.paper_fallback_reason = reason
    if placement_error:
        bet.placement_error = placement_error
    bet.clob_accept_status = "paper_fallback"
    bet.order_status = "filled"  # so _is_bet_executable() picks it up at settle
    bet.order_id = f"paper_fallback_{int(_now_ts() * 1000)}"
    bet.order_placed_at = _now_iso()

    # Synthesize fill at the limit price. The settlement code reads
    # actual_fill_price (preferred) or fill_price as the cost basis, and
    # multiplies fill_size by 1.0 (Over wins) or 0.0 (Over loses). All
    # numbers below mirror what a clean real fill would have written.
    bet.actual_fill_price = bet.limit_price
    bet.fill_price = bet.limit_price
    bet.filled_at = _now_iso()
    if (bet.order_size_shares is None or bet.order_size_shares <= 0) and bet.limit_price > 0:
        bet.order_size_shares = round(bet.stake / bet.limit_price, 4)
    bet.fill_size = bet.order_size_shares
    bet.filled_shares = bet.order_size_shares
    bet.fill_cost = round(bet.stake, 2)
    bet.fill_cost_usdc = round(bet.stake, 2)

    engine._paper_fallback_stats["placed"] = int(
        engine._paper_fallback_stats.get("placed", 0)
    ) + 1
    engine._paper_fallback_stats["total_stake"] = round(
        float(engine._paper_fallback_stats.get("total_stake", 0.0)) + float(bet.stake), 2
    )

    LOGGER.warning(
        "PAPER FALLBACK [%s] %s@%s O/U %s OVER  reason=%s  limit=%.3f  "
        "stake=$%.2f  shares=%.4f -- synthesized fill, will settle on game final",
        bet.bet_id, bet.away_abbrev, bet.home_abbrev, bet.line, reason,
        bet.limit_price, bet.stake, bet.order_size_shares or 0.0,
    )

    engine._append_to_live_ledger(bet)
    engine._save_session()
    return bet


def place_bet(
    engine: "LiveTradingEngine",
    *,
    game,
    market,
    best_ask: float,
    fair_value: float,
    base_fair_value: float,
    stage2_run_env_delta: float,
    team_offense_delta: float,
    edge: float,
    inferred_runs: int,
    inning: int,
    inning_state: str,
    outs: int,
    away_score_before: int,
    home_score_before: int,
    batting_is_away: bool,
    runners_on: int,
    decision_bid: Optional[float] = None,
    ltp: Optional[float] = None,
    state_value_diagnostics: Optional[Dict[str, object]] = None,
    side: str = "over",
) -> Optional[BetRecord]:
    """Place a real limit BUY order on the Polymarket CLOB.

    Mirrors SignalEngine._place_bet() for record-keeping, then posts
    a live limit order via CLOBOrderClient.

    2026-05-28: parameterized by ``side``. For ``side="over"`` (default)
    behavior is unchanged: trade the over outcome token. For
    ``side="under"`` the caller passes UNDER-side inputs (under FV, under
    ask, under edge, under bid) and the order is posted on
    ``market.under_token_id``. Budget / per-game / max-open-order caps are
    shared across both sides (total exposure); the correlated-line cap is
    evaluated per side. The EV-policy gate is OVER-only (the runtime EV
    artifact is a score-event OVER model) and is skipped for UNDER.
    Downstream lifecycle (fill polling, cancel, reconcile) routes on the
    side-correct token via ``models.bet_traded_token_id``.
    """
    engine._last_place_bet_skip_reason = None
    line_val = float(market.line)
    half = _inning_state_to_half(inning_state)
    side = str(side or "over").lower()
    is_under = side == "under"
    traded_token_id = (
        str(getattr(market, "under_token_id", "") or "")
        if is_under
        else str(getattr(market, "over_token_id", "") or "")
    )
    if is_under and not traded_token_id:
        LOGGER.info(
            "Skip %s@%s line=%.1f UNDER: market has no under_token_id",
            game.away_abbrev, game.home_abbrev, line_val,
        )
        engine._last_place_bet_skip_reason = "no_under_token"
        return None
    side_label = side.upper()

    # --- Compute limit price from a fresh executable book ---
    # Keep best_ask as the immutable decision-time ask; use the refreshed
    # bid/ask only for execution so we do not mix stale signal prices with
    # current book depth.
    try:
        book = engine._fetch_depth_snapshot(traded_token_id, engine.trade_args.capture_depth)
    except Exception as exc:
        LOGGER.info(
            "Skip %s@%s line=%.1f %s: fresh execution book unavailable (%s)",
            game.away_abbrev, game.home_abbrev, line_val, side_label, exc,
        )
        engine._last_place_bet_skip_reason = "fresh_book_unavailable"
        return None

    bid = book.get("best_bid")
    execution_ask = book.get("best_ask")
    if not book.get("ok") or bid is None or execution_ask is None:
        LOGGER.info(
            "Skip %s@%s line=%.1f: fresh execution book missing bid/ask "
            "(ok=%s bid=%s ask=%s error=%s)",
            game.away_abbrev, game.home_abbrev, line_val,
            book.get("ok"), bid, execution_ask, book.get("error"),
        )
        engine._last_place_bet_skip_reason = "fresh_book_unavailable"
        return None

    execution_spread = execution_ask - bid
    if bid >= execution_ask or execution_spread > engine.trade_args.max_spread:
        LOGGER.info(
            "Skip %s@%s line=%.1f: fresh execution book invalid "
            "(bid=%.3f ask=%.3f spread=%.3f max=%.3f)",
            game.away_abbrev, game.home_abbrev, line_val,
            bid, execution_ask, execution_spread, engine.trade_args.max_spread,
        )
        engine._last_place_bet_skip_reason = "fresh_book_invalid"
        return None

    limit_price = engine._compute_limit_price(
        ask=execution_ask,
        bid=bid,
        fair_value=fair_value,
        line_val=line_val,
    )
    if limit_price is None:
        LOGGER.debug(
            "Skip %s@%s line=%.1f: cannot place resting limit while preserving min edge "
            "(ask=%.3f bid=%.3f fv=%.3f)",
            game.away_abbrev, game.home_abbrev, line_val, execution_ask, bid, fair_value,
        )
        engine._last_place_bet_skip_reason = "limit_unplaceable"
        return None

    # --- Stake sizing ---
    stake_mode = getattr(engine.live_args, "stake_mode", DEFAULT_STAKE_MODE)
    kelly_raw_full = kelly_full = kelly_edge_used = None
    if stake_mode == "kelly":
        kelly_raw_full, kelly_full, kelly_edge_used = engine._kelly_components(fair_value, limit_price)
    base_stake = engine._compute_stake(fair_value, limit_price)
    stake = base_stake

    # --- Calibrated-edge stake scaling (Active #6 part 2, 2026-05-12) ---
    # Multiply base stake by a [min, max] multiplier derived from the
    # calibrated edge. Shadow mode records the multiplier on the bet
    # record without applying; enforce mode applies it.
    cs_mode = str(getattr(
        engine.live_args, "calibrated_stake_scale_mode",
        DEFAULT_CALIBRATED_STAKE_SCALE_MODE,
    )).lower()
    cs_multiplier: Optional[float] = None
    cs_edge: Optional[float] = None
    cs_applied = False
    if cs_mode in ("shadow", "enforce"):
        state_diag = state_value_diagnostics or {}
        family_for_cal = str(
            state_diag.get("state_value_strategy") or SCORE_EVENT_TRANSITION
        )
        cs_edge = resolve_calibrated_edge(
            engine,
            raw_or_final_fv=fair_value,
            decision_ask=best_ask,
            model_family=family_for_cal,
            line=line_val,
        )
        if cs_edge is not None:
            cs_multiplier = calibrated_stake_multiplier(
                calibrated_edge=cs_edge,
                min_multiplier=float(getattr(
                    engine.live_args, "calibrated_stake_min_multiplier",
                    DEFAULT_CALIBRATED_STAKE_MIN_MULTIPLIER,
                )),
                max_multiplier=float(getattr(
                    engine.live_args, "calibrated_stake_max_multiplier",
                    DEFAULT_CALIBRATED_STAKE_MAX_MULTIPLIER,
                )),
                ramp_top_edge=float(getattr(
                    engine.live_args, "calibrated_stake_ramp_top_edge",
                    DEFAULT_CALIBRATED_STAKE_RAMP_TOP_EDGE,
                )),
            )
            if cs_mode == "enforce":
                stake = round(base_stake * cs_multiplier, 2)
                cs_applied = True
            LOGGER.info(
                "Calibrated-stake [%s] %s@%s line=%.1f calibrated_edge=%+.4f "
                "multiplier=%.3f base=$%.2f -> stake=$%.2f%s",
                cs_mode.upper(),
                game.away_abbrev, game.home_abbrev, line_val,
                cs_edge, cs_multiplier, base_stake, stake,
                "" if cs_applied else " (shadow only -- stake unchanged)",
            )

    if stake < engine.live_args.min_order_size:
        LOGGER.info(
            "Computed stake $%.2f below Polymarket minimum $%.2f -- skipping %s@%s line=%s",
            stake, engine.live_args.min_order_size,
            game.away_abbrev, game.home_abbrev, market.line,
        )
        engine._last_place_bet_skip_reason = "stake_below_min_order"
        return None

    # --- EV policy gate (optional, OVER-only) ---
    # The runtime EV-policy artifact is a score-event OVER win/fill model;
    # it has no UNDER features. Skip the gate entirely for UNDER bets.
    if is_under:
        ev_allow, ev_diag = True, None
    else:
        feature_bid = decision_bid if decision_bid is not None else bid
        ev_feature_row = engine._build_ev_feature_row(
            game=game, market=market, line_val=line_val,
            best_ask=best_ask, bid=feature_bid, fair_value=fair_value,
            base_fair_value=base_fair_value,
            stage2_run_env_delta=stage2_run_env_delta,
            team_offense_delta=team_offense_delta,
            edge=edge, inferred_runs=inferred_runs,
            inning=inning, inning_state=inning_state, outs=outs,
            away_score_before=away_score_before, home_score_before=home_score_before,
            runners_on=runners_on, limit_price=limit_price, stake=stake,
            ltp=ltp, execution_book=book,
            state_value_diagnostics=state_value_diagnostics,
        )
        ev_allow, ev_diag = engine._evaluate_ev_policy(ev_feature_row, stake, limit_price)
        if engine._ev_policy_mode == "shadow" and ev_diag:
            LOGGER.info(
                "EV policy [%s] %s@%s line=%.1f allow=%s p_win=%.3f p_fill=%.3f "
                "ev_if_filled=%.2f ev_per_stake=%.4f",
                "SHADOW",
                game.away_abbrev, game.home_abbrev, line_val,
                ev_allow,
                ev_diag.get("p_win_if_filled", 0),
                ev_diag.get("p_fill", 0),
                ev_diag.get("ev_if_filled", 0),
                ev_diag.get("ev_per_stake", 0),
            )
        elif engine._ev_policy_mode == "enforce" and not ev_allow:
            LOGGER.info(
                "EV policy BLOCK %s@%s line=%.1f reason=%s ev_per_stake=%.4f < min=%.4f",
                game.away_abbrev, game.home_abbrev, line_val,
                ev_diag.get("reason", "threshold"),
                ev_diag.get("ev_per_stake", 0),
                ev_diag.get("min_ev_per_stake", 0),
            )
            engine._last_place_bet_skip_reason = "ev_policy_block"
            return None

    # --- Budget checks ---
    deployed = sum(
        engine._filled_notional(b) for b in engine._bets
        if getattr(b, "order_status", "") == "filled"
    )
    reserved = sum(
        b.stake for b in engine._bets
        if _is_exposure_counted_status(getattr(b, "order_status", "")) and b.stake
    )
    actual_exposure = deployed + reserved
    remaining_budget = engine.live_args.daily_budget - actual_exposure
    if stake > remaining_budget:
        LOGGER.info(
            "Daily budget $%.0f reached -- deployed $%.0f  reserved $%.0f  "
            "remaining $%.0f  needed $%.2f  skipping %s@%s line=%s",
            engine.live_args.daily_budget, deployed, reserved,
            remaining_budget, stake,
            game.away_abbrev, game.home_abbrev, market.line,
        )
        engine._last_place_bet_skip_reason = "budget_exhausted"
        return None

    # Per-game budget cap
    per_game_frac = getattr(engine.live_args, "per_game_budget_fraction", DEFAULT_PER_GAME_BUDGET_FRACTION)
    per_game_cap = engine.live_args.daily_budget * per_game_frac
    game_deployed = sum(
        engine._filled_notional(b) for b in engine._bets
        if b.game_pk == game.game_pk and getattr(b, "order_status", "") == "filled"
    )
    game_reserved = sum(
        b.stake for b in engine._bets
        if b.game_pk == game.game_pk
        and _is_exposure_counted_status(getattr(b, "order_status", ""))
        and b.stake
    )
    game_exposure = game_deployed + game_reserved
    if game_exposure + stake > per_game_cap:
        LOGGER.info(
            "Per-game cap $%.0f reached for %s@%s (exposure=$%.2f) -- skipping line=%s",
            per_game_cap, game.away_abbrev, game.home_abbrev, game_exposure, market.line,
        )
        engine._last_place_bet_skip_reason = "per_game_cap"
        return None

    # --- Correlated-line cap (Active #6, 2026-05-12) ---
    # Multiple over-side bets on the same game share the same trade idea
    # (a run scored / about to score), so their outcomes are highly
    # correlated. See `LiveTradingEngine._evaluate_correlated_line_cap`.
    correlated_skip = engine._evaluate_correlated_line_cap(game=game, market=market, side=side)
    if correlated_skip is not None:
        engine._last_place_bet_skip_reason = correlated_skip
        return None

    # --- Max open orders ---
    if len(engine._open_orders) >= engine.live_args.max_open_orders:
        LOGGER.info(
            "Max open orders (%d) reached -- skipping %s@%s line=%s",
            engine.live_args.max_open_orders,
            game.away_abbrev, game.home_abbrev, market.line,
        )
        engine._last_place_bet_skip_reason = "max_open_orders"
        return None

    # --- Create bet record ---
    engine._bet_counter += 1
    bet_id = f"{engine.date_str}_{game.game_pk}_{market.line}_{engine._bet_counter:04d}"
    inf_away = away_score_before + inferred_runs if (half == "T") else away_score_before
    inf_home = home_score_before if (half == "T") else home_score_before + inferred_runs
    state_diag = state_value_diagnostics or {}

    def _diag_float(name: str) -> Optional[float]:
        value = state_diag.get(name)
        if value is None or value == "":
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def _diag_str(name: str) -> str:
        return str(state_diag.get(name) or "")

    def _diag_bool(name: str) -> Optional[bool]:
        value = state_diag.get(name)
        if value is None or value == "":
            return None
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        text = str(value).strip().lower()
        if text in {"true", "1", "yes", "y"}:
            return True
        if text in {"false", "0", "no", "n"}:
            return False
        return None

    def _diag_int(name: str) -> Optional[int]:
        value = state_diag.get(name)
        if value is None or value == "":
            return None
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return None

    bet = LiveBetRecord(
        bet_id=bet_id,
        placed_at=_now_iso(),
        game_pk=game.game_pk,
        away_abbrev=game.away_abbrev,
        home_abbrev=game.home_abbrev,
        line=market.line,
        side=side,
        entry_ask=best_ask,
        decision_ask=best_ask,
        execution_bid=bid,
        execution_ask=execution_ask,
        fair_value=fair_value,
        base_fair_value=base_fair_value,
        stage2_run_env_delta=round(stage2_run_env_delta, 4),
        team_offense_delta=round(team_offense_delta, 4),
        edge=edge,
        inferred_runs=inferred_runs,
        inning=inning,
        inning_state=inning_state,
        outs=outs,
        away_score_before=away_score_before,
        home_score_before=home_score_before,
        inferred_away_after=inf_away,
        inferred_home_after=inf_home,
        stake=stake,
        runners_on=runners_on,
        venue_name=game.venue_name,
        ltp_at_signal=ltp,
        config_label=str(getattr(engine.trade_args, "config_label", "default") or "default"),
        inferred_state_base_poisson=_diag_float("inferred_state_base_poisson"),
        inferred_state_base_empirical=_diag_float("inferred_state_base_empirical"),
        inferred_state_poisson_minus_empirical=_diag_float("inferred_state_poisson_minus_empirical"),
        inferred_state_empirical_edge=_diag_float("inferred_state_empirical_edge"),
        inferred_state_n=_diag_float("inferred_state_n"),
        inferred_state_n_samples=_diag_float("inferred_state_n_samples"),
        inferred_state_weighted_n=_diag_float("inferred_state_weighted_n"),
        inferred_state_effective_n=_diag_float("inferred_state_effective_n"),
        inferred_state_effective_n_proxy=_diag_float("inferred_state_effective_n_proxy"),
        inferred_state_stage1_trust_weight=_diag_float("inferred_state_stage1_trust_weight"),
        inferred_state_stage1_support_bucket=_diag_str("inferred_state_stage1_support_bucket"),
        inferred_state_exact_cell_support=_diag_bool("inferred_state_exact_cell_support"),
        inferred_state_poisson_line_exact=_diag_bool("inferred_state_poisson_line_exact"),
        inferred_state_empirical_line_exact=_diag_bool("inferred_state_empirical_line_exact"),
        inferred_state_empirical_sample_support=_diag_float("inferred_state_empirical_sample_support"),
        inferred_state_empirical_sample_bucket=_diag_str("inferred_state_empirical_sample_bucket"),
        inferred_state_state_fallback_penalty=_diag_float("inferred_state_state_fallback_penalty"),
        inferred_state_line_fallback_penalty=_diag_float("inferred_state_line_fallback_penalty"),
        inferred_state_fallback_level=_diag_int("inferred_state_fallback_level"),
        inferred_state_fallback_label=_diag_str("inferred_state_fallback_label"),
        inferred_state_cell_key=_diag_str("inferred_state_cell_key"),
        inferred_state_line_key_poisson=_diag_str("inferred_state_line_key_poisson"),
        inferred_state_line_key_empirical=_diag_str("inferred_state_line_key_empirical"),
        inferred_state_line_fallback_mode=_diag_str("inferred_state_line_fallback_mode"),
        inferred_state_empirical_line_fallback_mode=_diag_str("inferred_state_empirical_line_fallback_mode"),
        inferred_state_empirical_line_source_key=_diag_str("inferred_state_empirical_line_source_key"),
        inferred_state_empirical_line_source_key_low=_diag_str("inferred_state_empirical_line_source_key_low"),
        inferred_state_empirical_line_source_key_high=_diag_str("inferred_state_empirical_line_source_key_high"),
        inferred_state_used_fallback=_diag_bool("inferred_state_used_fallback"),
        inferred_state_base_source=_diag_str("inferred_state_base_source"),
        home_leading_late=_diag_bool("home_leading_late"),
        batting_team_is_home=_diag_bool("batting_team_is_home"),
        bottom9_available_if_needed=_diag_bool("bottom9_available_if_needed"),
        expected_remaining_half_innings=_diag_float("expected_remaining_half_innings"),
        expected_remaining_pa_bucket=_diag_str("expected_remaining_pa_bucket"),
        home_skip_bottom9_risk=_diag_float("home_skip_bottom9_risk"),
        scoring_path_available=_diag_bool("scoring_path_available"),
        scoring_path_innings_observed=_diag_int("scoring_path_innings_observed"),
        scoring_path_runs_observed=_diag_int("scoring_path_runs_observed"),
        scoring_path_inning_runs=_diag_str("scoring_path_inning_runs"),
        scoring_inning_rate=_diag_float("scoring_inning_rate"),
        scoring_half_rate=_diag_float("scoring_half_rate"),
        burst_share=_diag_float("burst_share"),
        scoreless_streak=_diag_int("scoreless_streak"),
        recent2_run_share=_diag_float("recent2_run_share"),
        weighted_run_inning_norm=_diag_float("weighted_run_inning_norm"),
        inning_run_slope=_diag_float("inning_run_slope"),
        state_value_strategy=_diag_str("state_value_strategy"),
        current_state_value_edge=_diag_float("current_state_value_edge"),
        current_state_value_fv_raw=_diag_float("current_state_value_fv_raw"),
        current_state_value_empirical_edge=_diag_float("current_state_value_empirical_edge"),
        current_state_value_effective_n_proxy=_diag_float("current_state_value_effective_n_proxy"),
        current_state_value_stage1_trust_weight=_diag_float("current_state_value_stage1_trust_weight"),
        current_state_value_stage1_support_bucket=_diag_str("current_state_value_stage1_support_bucket"),
        current_state_value_exact_cell_support=_diag_bool("current_state_value_exact_cell_support"),
        current_state_value_poisson_line_exact=_diag_bool("current_state_value_poisson_line_exact"),
        current_state_value_empirical_line_exact=_diag_bool("current_state_value_empirical_line_exact"),
        current_state_value_empirical_sample_support=_diag_float("current_state_value_empirical_sample_support"),
        current_state_value_empirical_sample_bucket=_diag_str("current_state_value_empirical_sample_bucket"),
        current_state_value_state_fallback_penalty=_diag_float("current_state_value_state_fallback_penalty"),
        current_state_value_line_fallback_penalty=_diag_float("current_state_value_line_fallback_penalty"),
        shadow_fv_inferred_lift=_diag_float("shadow_fv_inferred_lift"),
        shadow_no_event_edge=_diag_float("shadow_no_event_edge"),
        shadow_after_event_edge=_diag_float("shadow_after_event_edge"),
        shadow_p_score_event_proxy=_diag_float("shadow_p_score_event_proxy"),
        shadow_phantom_risk_score=_diag_float("shadow_phantom_risk_score"),
        shadow_phantom_risk_band=_diag_str("shadow_phantom_risk_band"),
        shadow_transition_model=_diag_str("shadow_transition_model"),
        shadow_low_ask_high_edge=_diag_bool("shadow_low_ask_high_edge"),
        shadow_runs_needed_exact_3p5=_diag_bool("shadow_runs_needed_exact_3p5"),
        shadow_current_state_edge_bucket=_diag_str("shadow_current_state_edge_bucket"),
        shadow_phantom_risk_bucket=_diag_str("shadow_phantom_risk_bucket"),
        shadow_current_phantom_combo_bucket=_diag_str("shadow_current_phantom_combo_bucket"),
        shadow_inning_bucket=_diag_str("shadow_inning_bucket"),
        shadow_inning_runs_needed_bucket=_diag_str("shadow_inning_runs_needed_bucket"),
        shadow_bottom9_home_lead_context=_diag_str("shadow_bottom9_home_lead_context"),
        shadow_home_skip_bottom9_risk_bucket=_diag_str("shadow_home_skip_bottom9_risk_bucket"),
        limit_price=limit_price,
        posted_limit=limit_price,
        order_size_shares=stake / limit_price if limit_price > 0 else None,
        over_token_id=str(getattr(market, "over_token_id", "") or ""),
        under_token_id=(str(getattr(market, "under_token_id", "") or "") if is_under else ""),
        stake_mode=stake_mode,
        kelly_full_fraction=round(kelly_full, 4) if kelly_full is not None else None,
        kelly_fraction_used=getattr(engine.live_args, "kelly_fraction", None) if stake_mode == "kelly" else None,
        calibrated_stake_mode=cs_mode,
        calibrated_stake_base_stake=round(base_stake, 4) if cs_multiplier is not None else None,
        calibrated_stake_multiplier=round(cs_multiplier, 4) if cs_multiplier is not None else None,
        calibrated_stake_edge_used=round(cs_edge, 6) if cs_edge is not None else None,
        calibrated_stake_applied=cs_applied if cs_multiplier is not None else None,
        **signal_context_fields(game),
    )
    engine._bets.append(bet)

    spread = execution_spread
    rn = line_val - (away_score_before + home_score_before)

    # --- Log LIVE BET ---
    if stake_mode == "kelly":
        kelly_frac     = getattr(engine.live_args, "kelly_fraction", DEFAULT_KELLY_FRACTION)
        kelly_cap      = getattr(engine.live_args, "kelly_max_bet_fraction", DEFAULT_KELLY_MAX_BET_FRACTION)
        kelly_edge_cap = max(0.0, getattr(engine.live_args, "kelly_max_edge", DEFAULT_KELLY_MAX_EDGE))
        LOGGER.info(
            "LIVE BET [%s] %s@%s O/U %.1f %s | "
            "decision_ask=%.3f  exec_bid=%.3f  exec_ask=%.3f  spread=%.3f  "
            "ltp=%s  limit=%.3f | "
            "FV=%.3f (base=%.3f S2=%+.3f S3=%+.3f) | "
            "edge_ask=%.3f  edge_limit=%.3f | "
            "inn=%s%d  outs=%d  score=%d-%d  rn=%.1f  runners=%d | "
            "kelly_f*=%.3f->%.3f  edge_cap=%.3f  frac=%.2f  stake=$%.2f  "
            "shares=%.4f  cap=$%.2f",
            bet_id,
            game.away_abbrev, game.home_abbrev, line_val, side_label,
            best_ask, bid, execution_ask, spread, f"{ltp:.3f}" if ltp is not None else "n/a", limit_price,
            fair_value, base_fair_value, stage2_run_env_delta, team_offense_delta,
            edge, fair_value - limit_price,
            half, inning, outs,
            away_score_before, home_score_before, rn, runners_on,
            kelly_raw_full or 0, kelly_full or 0, kelly_edge_cap, kelly_frac, stake,
            bet.order_size_shares or 0,
            engine.live_args.daily_budget * kelly_cap,
        )
    else:
        LOGGER.info(
            "LIVE BET [%s] %s@%s O/U %.1f %s | "
            "decision_ask=%.3f  exec_bid=%.3f  exec_ask=%.3f  spread=%.3f  "
            "ltp=%s  limit=%.3f | "
            "FV=%.3f (base=%.3f S2=%+.3f S3=%+.3f) | "
            "edge=%.3f | "
            "inn=%s%d  outs=%d  score=%d-%d  rn=%.1f  runners=%d | "
            "stake=$%.2f shares=%.4f (flat)",
            bet_id,
            game.away_abbrev, game.home_abbrev, line_val, side_label,
            best_ask, bid, execution_ask, spread, f"{ltp:.3f}" if ltp is not None else "n/a", limit_price,
            fair_value, base_fair_value, stage2_run_env_delta, team_offense_delta,
            edge,
            half, inning, outs,
            away_score_before, home_score_before, rn, runners_on,
            stake, bet.order_size_shares or 0,
        )

    # --- Post order to CLOB ---
    if engine._dry_run:
        dry_id = f"dry_run_{int(_now_ts() * 1000)}"
        bet.order_id = dry_id
        bet.order_placed_at = _now_iso()
        bet.order_status = "live"
        LOGGER.info(
            "DRY RUN: would post limit BUY %.3f x%.4f shares (max_cost=$%.2f) to CLOB for [%s]",
            limit_price, bet.order_size_shares or 0, stake, bet_id,
        )
        engine._open_orders[dry_id] = bet
        engine._append_to_live_ledger(bet)
        engine._save_session()
        return bet

    # Wallet-cooldown short-circuit: if a recent CLOB rejection tripped
    # the cooldown, skip the CLOB and route directly to paper-fallback.
    # Real placement resumes when the cooldown elapses.
    cooldown_remaining = _wallet_cooldown_remaining(engine)
    if cooldown_remaining > 0:
        engine._last_place_bet_skip_reason = "wallet_cooldown"
        LOGGER.info(
            "Wallet cooldown active (%.0fs remaining) -- routing [%s] to paper-fallback "
            "without attempting CLOB placement.",
            cooldown_remaining, bet_id,
        )
        return _record_paper_fallback_bet(engine, bet, reason="wallet_cooldown")

    try:
        result = engine._clob.place_limit_buy(
            token_id=traded_token_id,
            price=limit_price,
            size_usdc=stake,
        )
    except Exception as exc:
        # Catch any unexpected API/parameter errors so the bet record is
        # correctly marked "error" rather than left as "pending" (which
        # would lock the stake in the budget for the rest of the session).
        bet.order_status = "error"
        engine._last_place_bet_skip_reason = "order_exception"
        LOGGER.error(
            "ORDER EXCEPTION [%s]: %s -- bet marked error, stake released from budget",
            bet_id, exc,
        )
        engine._append_to_live_ledger(bet)
        engine._save_session()
        return None
    bet.order_placed_at = _now_iso()

    if result.success and result.order_id:
        bet.order_id = result.order_id
        bet.clob_accept_status = result.status
        bet.order_status = _normalize_accepted_order_status(result.status)
        if result.size_shares is not None:
            bet.order_size_shares = result.size_shares
        if _is_exposure_counted_status(bet.order_status):
            engine._open_orders[result.order_id] = bet
        LOGGER.info(
            "ORDER PLACED [%s] order_id=%s  status=%s raw_status=%s  sign_ms=%.0f  post_ms=%.0f",
            bet_id, result.order_id, bet.order_status, result.status, result.sign_ms, result.post_ms,
        )
    else:
        # Preserve the raw SDK error message structurally on the bet so
        # later audits can answer "what went wrong?" without log-grep.
        # Truncate generously so a verbose Polymarket payload can't
        # balloon the session JSON.
        error_text = str(result.error or "unknown_error")[:500]
        # Wallet-balance shortfall: route to paper-fallback so we keep the
        # signal/outcome data instead of dropping the bet, and trip a
        # cooldown so we don't burn the next ~60s of CLOB quota retrying.
        if _is_balance_error(result.error):
            _trip_wallet_cooldown(engine, reason="clob_balance_error")
            engine._last_place_bet_skip_reason = "wallet_balance_error"
            LOGGER.error(
                "ORDER FAILED [%s]: %s  sign_ms=%.0f  post_ms=%.0f -- "
                "routing to paper-fallback.",
                bet_id, result.error, result.sign_ms, result.post_ms,
            )
            return _record_paper_fallback_bet(
                engine, bet,
                reason="clob_balance_error",
                placement_error=error_text,
            )
        bet.order_status = "error"
        bet.placement_error = error_text
        engine._last_place_bet_skip_reason = "order_failed"
        LOGGER.error(
            "ORDER FAILED [%s]: %s  sign_ms=%.0f  post_ms=%.0f",
            bet_id, result.error, result.sign_ms, result.post_ms,
        )
        engine._append_to_live_ledger(bet)
        engine._save_session()
        return None

    engine._append_to_live_ledger(bet)
    engine._save_session()
    return bet
