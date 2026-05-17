#!/usr/bin/env python3
"""
signal_pipeline_gates_pre_fv.py -- Gate phases that fire BEFORE FV inference.

Free functions extracted from signal_pipeline.py (Tier 4 refactor, 2026-05-01).
Hosts the two gate phases that run before any model work:

  1. evaluate_early_gates (Gates 1-5, environmental)
       Inning, inactive-inning-state, min_entry_ask, spread, ask-jump confirm.
  2. evaluate_pre_inference_gates (Gates 6-8e, game-state)
       min_current_total, runs_pace, runs_needed, close-game RN dead zone,
       inning-5/6 dead zones, blowout / blowout-adjacent.

Conceptual cut: "do we even bother running the model?" If any of these gates
fires, the tick stops and writes a skip row. Post-FV gates ("does the model
output justify a trade?") live in signal_pipeline_gates_post_fv.py.

Engine attrs read:
  - engine.trade_args.* (many; see individual gates)
  - engine._shadow_gate1_blocked / _would_pass, _shadow_gate3_*
Engine method calls (preserved as method calls so subclass overrides work):
  - engine._log_skip_debug_once(...)
  - engine._min_current_total_relax_pass(...)
  - engine._blowout_relax_pass(...)
  - engine._resolve_relax_mode_action(...)
"""

from __future__ import annotations

from typing import Callable, Dict, TYPE_CHECKING

from line_state import _runs_pace_ok
from signal_config import (
    DEFAULT_GATE_BLOWOUT_RELAX_MODE,
    DEFAULT_GATE_MIN_CURRENT_TOTAL_RELAX_MODE,
    DEFAULT_SHADOW_RELAXED_MIN_INNING_HIGH_LINE_OFFSET,
    DEFAULT_SHADOW_RELAXED_MIN_INNING_OFFSET,
    INACTIVE_INNING_STATES,
)
from signal_pipeline_payload import record_early_skip as _record_early_skip

if TYPE_CHECKING:
    from signal_engine import SignalEngine
    from signal_pipeline import PreInferenceGateResult, TickContext


def evaluate_early_gates(self: "SignalEngine", ctx: "TickContext") -> bool:
    """Evaluate Gates 1-5. Return True when the tick should stop here."""
    game = ctx.game
    market = ctx.market
    state = ctx.state
    inning = ctx.inning
    inning_state = ctx.inning_state
    outs = ctx.outs
    current_total = ctx.current_total
    line_val = ctx.line_val
    best_bid = ctx.best_bid
    ask = ctx.ask

    # --- GATE 1: Inning filter (line-aware) ---
    # [TR3 Change 5] High lines require a later inning minimum
    if line_val >= self.trade_args.high_line_cutoff:
        if inning < self.trade_args.min_inning_high_line:
            self._shadow_gate1_blocked += 1
            relaxed_floor = self.trade_args.min_inning_high_line - int(getattr(
                self.trade_args, "shadow_relaxed_min_inning_high_line_offset",
                DEFAULT_SHADOW_RELAXED_MIN_INNING_HIGH_LINE_OFFSET,
            ))
            if inning >= relaxed_floor:
                self._shadow_gate1_would_pass += 1
            _record_early_skip(
                self,
                ctx,
                "gate_min_inning",
                {
                    "min_inning_effective": self.trade_args.min_inning_high_line,
                    "high_line_cutoff": self.trade_args.high_line_cutoff,
                    "shadow_relaxed_evaluated": True,
                    "shadow_relaxed_would_pass": inning >= relaxed_floor,
                    "shadow_relaxed_reason": "gate_min_inning",
                    "shadow_relaxed_value": inning,
                    "shadow_relaxed_threshold": relaxed_floor,
                    "shadow_relaxed_comparator": ">=",
                },
            )
            return True
    else:
        if inning < self.trade_args.min_inning:
            self._shadow_gate1_blocked += 1
            relaxed_floor = self.trade_args.min_inning - int(getattr(
                self.trade_args, "shadow_relaxed_min_inning_offset",
                DEFAULT_SHADOW_RELAXED_MIN_INNING_OFFSET,
            ))
            if inning >= relaxed_floor:
                self._shadow_gate1_would_pass += 1
            _record_early_skip(
                self,
                ctx,
                "gate_min_inning",
                {
                    "min_inning_effective": self.trade_args.min_inning,
                    "high_line_cutoff": self.trade_args.high_line_cutoff,
                    "shadow_relaxed_evaluated": True,
                    "shadow_relaxed_would_pass": inning >= relaxed_floor,
                    "shadow_relaxed_reason": "gate_min_inning",
                    "shadow_relaxed_value": inning,
                    "shadow_relaxed_threshold": relaxed_floor,
                    "shadow_relaxed_comparator": ">=",
                },
            )
            return True

    # --- GATE 2: Inactive inning state (TR2 Change 4) ---
    if inning_state.lower() in INACTIVE_INNING_STATES:
        _record_early_skip(
            self,
            ctx,
            "gate_inactive_inning_state",
            {"inactive_inning_state": inning_state.lower()},
        )
        return True

    # --- GATE 3: Minimum entry ask (line-aware) ---
    # [TR5] High lines require a stricter ask floor (0.60 vs 0.55)
    min_ask = (
        self.trade_args.min_entry_ask_high_line
        if line_val >= self.trade_args.high_line_cutoff
        else self.trade_args.min_entry_ask
    )
    if ask < min_ask:
        self._shadow_gate3_blocked += 1
        # Relaxed: allow asks 5pp lower than strict floor
        relaxed_min_ask = min_ask - 0.05
        if ask >= relaxed_min_ask:
            self._shadow_gate3_would_pass += 1
        _record_early_skip(
            self,
            ctx,
            "gate_min_entry_ask",
            {
                "min_entry_ask_effective": min_ask,
                "shadow_relaxed_evaluated": True,
                "shadow_relaxed_would_pass": ask >= relaxed_min_ask,
                "shadow_relaxed_reason": "gate_min_entry_ask",
                "shadow_relaxed_value": ask,
                "shadow_relaxed_threshold": relaxed_min_ask,
                "shadow_relaxed_comparator": ">=",
            },
        )
        return True

    # --- GATE 4: Spread filter ---
    if best_bid is not None and best_bid >= ask:
        self._log_skip_debug_once(
            reason="gate_crossed_book",
            game=game,
            market=market,
            inning=inning,
            inning_state=inning_state,
            outs=outs,
            current_total=current_total,
            message="Skip %s@%s line=%s: crossed/locked book (bid=%.3f ask=%.3f)",
            args=(game.away_abbrev, game.home_abbrev, market.line, best_bid, ask),
        )
        _record_early_skip(
            self,
            ctx,
            "gate_crossed_book",
            {"crossed_book_bid": best_bid, "crossed_book_ask": ask},
        )
        return True
    if best_bid is not None and (ask - best_bid) > self.trade_args.max_spread:
        _record_early_skip(
            self,
            ctx,
            "gate_wide_spread",
            {
                "max_spread_effective": self.trade_args.max_spread,
                "spread": ask - best_bid,
            },
        )
        return True

    # --- GATE 5: Confirmed sustained ask jump (TR2 Changes 1+2) ---
    # [TR5 P1 fix] Pass configured lookback_ticks so the parameter is actually used
    ask_jump = state.ask_jump(self.trade_args.lookback_ticks)
    if not state.is_confirmed_signal(
        jump_threshold=self.trade_args.jump_threshold,
        confirmation_ticks=self.trade_args.confirmation_ticks,
        lookback=self.trade_args.lookback_ticks,
    ):
        if state.pending_signal or (ask_jump is not None and ask_jump >= self.trade_args.jump_threshold):
            _record_early_skip(
                self,
                ctx,
                "gate_ask_jump_unconfirmed",
                {
                    "ask_jump": ask_jump,
                    "jump_threshold_effective": self.trade_args.jump_threshold,
                    "lookback_ticks": self.trade_args.lookback_ticks,
                    "baseline_ask": state.baseline_ask,
                    "pending_jump_ask": state.pending_jump_ask,
                    "pending_ticks_remaining": state.pending_ticks_remaining,
                    "confirmation_ticks": self.trade_args.confirmation_ticks,
                    "confirmation_status": "pending" if state.pending_signal else "unconfirmed",
                },
            )
        return True

    return False


def evaluate_pre_inference_gates(
    self: "SignalEngine",
    ctx: "TickContext",
    candidate_payload: Dict[str, object],
    record_skip: Callable[..., None],
) -> "PreInferenceGateResult":
    """Evaluate Gates 6-8e. Return state needed by downstream inference."""
    # PreInferenceGateResult lives in signal_pipeline.py; imported lazily
    # to avoid a runtime circular import.
    from signal_pipeline import PreInferenceGateResult

    game = ctx.game
    market = ctx.market
    inning = ctx.inning
    inning_state = ctx.inning_state
    outs = ctx.outs
    away_score = ctx.away_score
    home_score = ctx.home_score
    current_total = ctx.current_total
    line_val = ctx.line_val
    ask = ctx.ask

    # --- GATE 6: Minimum current total (TR3 Change 1) ---
    if current_total < self.trade_args.min_current_total:
        relax_mode = str(
            getattr(
                self.trade_args,
                "gate_min_current_total_relax_mode",
                DEFAULT_GATE_MIN_CURRENT_TOTAL_RELAX_MODE,
            )
            or DEFAULT_GATE_MIN_CURRENT_TOTAL_RELAX_MODE
        ).strip().lower()
        relax_pass = self._min_current_total_relax_pass(
            inning=inning,
            current_total=current_total,
            ask=ask,
            lead_abs=abs(away_score - home_score),
            runs_needed=(line_val - current_total),
        )
        relax_apply, relax_arm = self._resolve_relax_mode_action(
            mode=relax_mode,
            gate_name="gate_min_current_total",
            game_pk=game.game_pk,
            line=market.line,
            relax_pass=relax_pass,
        )
        candidate_payload.update(
            {
                "conditional_relax_gate": "gate_min_current_total",
                "conditional_relax_mode": relax_mode,
                "conditional_relax_arm": relax_arm,
                "conditional_relax_would_pass": relax_pass,
                "conditional_relax_applied": relax_apply,
            }
        )
        if relax_apply:
            self._log_skip_debug_once(
                reason="gate_min_current_total_relax_applied",
                game=game,
                market=market,
                inning=inning,
                inning_state=inning_state,
                outs=outs,
                current_total=current_total,
                message="Gate 6 conditional relax APPLIED %s@%s line=%s: total=%d < min=%d "
                        "(mode=%s arm=%s ask=%.3f lead=%d rn=%.2f)",
                args=(
                    game.away_abbrev, game.home_abbrev, market.line,
                    current_total, self.trade_args.min_current_total,
                    relax_mode, relax_arm, ask, abs(away_score - home_score), (line_val - current_total),
                ),
            )
        else:
            self._log_skip_debug_once(
                reason="gate_min_current_total",
                game=game,
                market=market,
                inning=inning,
                inning_state=inning_state,
                outs=outs,
                current_total=current_total,
                message="Skip %s@%s line=%s: current_total=%d < min=%d (mode=%s arm=%s relax_pass=%s)",
                args=(
                    game.away_abbrev, game.home_abbrev, market.line,
                    current_total, self.trade_args.min_current_total,
                    relax_mode, relax_arm, relax_pass,
                ),
            )
            record_skip(
                "gate_min_current_total",
                shadow_values={
                    "current_total": current_total,
                    "inning": inning,
                    "decision_ask": ask,
                },
                secondary_shadow_reason="gate_min_current_total_conditional_v1",
                secondary_shadow_values={
                    "current_total": current_total,
                    "inning": inning,
                    "decision_ask": ask,
                    "lead_abs": abs(away_score - home_score),
                    "runs_needed": (line_val - current_total),
                },
                extra_fields={
                    "conditional_relax_gate": "gate_min_current_total",
                    "conditional_relax_mode": relax_mode,
                    "conditional_relax_arm": relax_arm,
                    "conditional_relax_would_pass": relax_pass,
                    "conditional_relax_applied": relax_apply,
                },
            )
            return PreInferenceGateResult(stopped=True)

    # --- GATE 7: Runs pace filter (TR3 Change 2) ---
    if not _runs_pace_ok(current_total, inning, market.line):
        self._log_skip_debug_once(
            reason="gate_runs_pace",
            game=game,
            market=market,
            inning=inning,
            inning_state=inning_state,
            outs=outs,
            current_total=current_total,
            message="Skip %s@%s line=%s: pace=(%.1f/9inn) insufficient for line=%.1f "
                    "(total=%d inn=%d)",
            args=(
                game.away_abbrev, game.home_abbrev, market.line,
                (current_total / inning) * 9, line_val,
                current_total, inning,
            ),
        )
        record_skip(
            "gate_runs_pace",
            shadow_values={
                "current_total": current_total,
                "inning": inning,
                "line": market.line,
            },
        )
        return PreInferenceGateResult(stopped=True)

    # --- GATE 8: Runs-needed gate (TR5, updated TR10) ---
    # Skip if too many runs are still needed - these contexts have strongly negative ROI.
    # [TR10] Lowered from 4.0 to 3.5: rn=3.5 live data = 3 bets 1W/2L -40.5% ROI
    runs_needed = line_val - current_total
    candidate_payload["runs_needed"] = runs_needed
    if runs_needed > self.trade_args.runs_needed_max:
        self._log_skip_debug_once(
            reason="gate_runs_needed_max",
            game=game,
            market=market,
            inning=inning,
            inning_state=inning_state,
            outs=outs,
            current_total=current_total,
            message="Skip %s@%s line=%s: runs_needed=%.1f > max=%.1f (total=%d)",
            args=(
                game.away_abbrev, game.home_abbrev, market.line,
                runs_needed, self.trade_args.runs_needed_max, current_total,
            ),
        )
        record_skip(
            "gate_runs_needed_max",
            shadow_values={"runs_needed": runs_needed},
        )
        return PreInferenceGateResult(stopped=True)

    # --- GATE 8b: Close-game + high runs-needed dead zone (TR6) ---
    # When the game is tied or within 1 run AND many runs are still needed,
    # scoring pace is suppressed: both managers play defense-first, deploy
    # closers early, and limit offense.
    lead = abs(away_score - home_score)
    candidate_payload["lead_abs"] = lead
    if lead < 2 and runs_needed >= self.trade_args.min_close_game_rn:
        self._log_skip_debug_once(
            reason="gate_close_game_runs_needed",
            game=game,
            market=market,
            inning=inning,
            inning_state=inning_state,
            outs=outs,
            current_total=current_total,
            message="Skip %s@%s line=%s: close game (lead=%d) + high rn=%.1f "
                    "(threshold %.1f) -- defense-first baseball context",
            args=(
                game.away_abbrev, game.home_abbrev, market.line,
                lead, runs_needed, self.trade_args.min_close_game_rn,
            ),
        )
        record_skip(
            "gate_close_game_runs_needed",
            shadow_values={"runs_needed": runs_needed, "lead_abs": lead},
        )
        return PreInferenceGateResult(stopped=True)

    # --- GATE 8c: Inning 5 bullpen-transition dead zone (TR8, updated TR10) ---
    if inning == 5 and runs_needed >= self.trade_args.inn5_rn_max:
        self._log_skip_debug_once(
            reason="gate_inning5_runs_needed",
            game=game,
            market=market,
            inning=inning,
            inning_state=inning_state,
            outs=outs,
            current_total=current_total,
            message="Skip %s@%s line=%s: inning 5 bullpen-transition dead zone "
                    "(rn=%.1f >= threshold %.1f) -- same rn at inn4 is 6W/0L",
            args=(
                game.away_abbrev, game.home_abbrev, market.line,
                runs_needed, self.trade_args.inn5_rn_max,
            ),
        )
        record_skip(
            "gate_inning5_runs_needed",
            shadow_values={"runs_needed": runs_needed, "inning": inning},
        )
        return PreInferenceGateResult(stopped=True)

    # --- GATE 8d: Inning 6 setup-reliever dead zone (TR9) ---
    if inning == 6 and runs_needed >= self.trade_args.inn6_rn_max:
        self._log_skip_debug_once(
            reason="gate_inning6_runs_needed",
            game=game,
            market=market,
            inning=inning,
            inning_state=inning_state,
            outs=outs,
            current_total=current_total,
            message="Skip %s@%s line=%s: inning 6 setup-reliever dead zone "
                    "(rn=%.1f >= threshold %.1f) -- 5yr backtest: 63%% WR on 1569 bets",
            args=(
                game.away_abbrev, game.home_abbrev, market.line,
                runs_needed, self.trade_args.inn6_rn_max,
            ),
        )
        record_skip(
            "gate_inning6_runs_needed",
            shadow_values={"runs_needed": runs_needed, "inning": inning},
        )
        return PreInferenceGateResult(stopped=True)

    # --- GATE 8e: Blowout shutout / blowout-adjacent gate (TR11, updated TR13) ---
    trailing_runs = min(away_score, home_score)
    blowout_lead = self.trade_args.blowout_lead_min
    blowout_adj_lead = self.trade_args.blowout_adj_lead_min
    strict_blowout_blocked = trailing_runs <= 1 and inning >= 6 and (
        lead >= blowout_lead
        or (lead >= blowout_adj_lead and inning >= 7)
    )
    if strict_blowout_blocked:
        relax_mode = str(
            getattr(
                self.trade_args,
                "gate_blowout_relax_mode",
                DEFAULT_GATE_BLOWOUT_RELAX_MODE,
            )
            or DEFAULT_GATE_BLOWOUT_RELAX_MODE
        ).strip().lower()
        relax_pass = self._blowout_relax_pass(
            inning=inning,
            ask=ask,
            runs_needed=runs_needed,
        )
        relax_apply, relax_arm = self._resolve_relax_mode_action(
            mode=relax_mode,
            gate_name="gate_blowout",
            game_pk=game.game_pk,
            line=market.line,
            relax_pass=relax_pass,
        )
        candidate_payload.update(
            {
                "conditional_relax_gate": "gate_blowout",
                "conditional_relax_mode": relax_mode,
                "conditional_relax_arm": relax_arm,
                "conditional_relax_would_pass": relax_pass,
                "conditional_relax_applied": relax_apply,
            }
        )
        if relax_apply:
            self._log_skip_debug_once(
                reason="gate_blowout_relax_applied",
                game=game,
                market=market,
                inning=inning,
                inning_state=inning_state,
                outs=outs,
                current_total=current_total,
                message="Gate 8e conditional relax APPLIED %s@%s line=%s "
                        "(mode=%s arm=%s trail=%d lead=%d inn=%d ask=%.3f rn=%.2f)",
                args=(
                    game.away_abbrev, game.home_abbrev, market.line,
                    relax_mode, relax_arm, trailing_runs, lead, inning, ask, runs_needed,
                ),
            )
        else:
            self._log_skip_debug_once(
                reason="gate_blowout",
                game=game,
                market=market,
                inning=inning,
                inning_state=inning_state,
                outs=outs,
                current_total=current_total,
                message="Skip %s@%s line=%s: blowout/blowout-adjacent gate "
                        "(trailing=%d lead=%d inn=%d blowout_min=%d adj_min=%d mode=%s arm=%s relax_pass=%s) "
                        "-- Poisson FV unreliable when trailing team shut out late",
                args=(
                    game.away_abbrev, game.home_abbrev, market.line,
                    trailing_runs, lead, inning, blowout_lead, blowout_adj_lead,
                    relax_mode, relax_arm, relax_pass,
                ),
            )
            record_skip(
                "gate_blowout",
                shadow_values={
                    "trailing_runs": trailing_runs,
                    "lead_abs": lead,
                    "inning": inning,
                    "decision_ask": ask,
                    "runs_needed": runs_needed,
                },
                secondary_shadow_reason="gate_blowout_conditional_v1",
                secondary_shadow_values={
                    "trailing_runs": trailing_runs,
                    "lead_abs": lead,
                    "inning": inning,
                    "decision_ask": ask,
                    "runs_needed": runs_needed,
                },
                extra_fields={
                    "conditional_relax_gate": "gate_blowout",
                    "conditional_relax_mode": relax_mode,
                    "conditional_relax_arm": relax_arm,
                    "conditional_relax_would_pass": relax_pass,
                    "conditional_relax_applied": relax_apply,
                },
            )
            return PreInferenceGateResult(stopped=True)

    return PreInferenceGateResult(
        stopped=False,
        runs_needed=runs_needed,
        lead=lead,
        trailing_runs=trailing_runs,
    )
