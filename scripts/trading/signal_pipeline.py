#!/usr/bin/env python3
"""
signal_pipeline.py -- Per-tick signal decision pipeline extracted from SignalEngine.

Behavior intentionally unchanged; this module hosts the large _process_tick logic
for better maintainability and smaller core engine context.
"""

from __future__ import annotations

import logging
import math
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Optional

PROJECT_DIR = Path(__file__).resolve().parents[2]

# Keep module import behavior aligned with signal_engine.py
sys.path.insert(0, str(PROJECT_DIR / "scripts" / "monitor"))
sys.path.insert(0, str(PROJECT_DIR / "scripts" / "analysis"))
sys.path.insert(0, str(PROJECT_DIR / "cache"))

from monitor_mlb_polymarket_ou import OUMarket, ScheduledGame, _safe_float  # noqa: E402,F401

from line_state import (  # noqa: E402,F401
    _ask_edge_boost,  # re-export for any external callers
    _now_iso,         # re-export; used by external callers
    _now_ts,
    _runs_pace_ok,    # re-export for any external callers
)
from signal_config import *  # noqa: F401,F403
from signal_pipeline_payload import (  # noqa: E402
    STATE_VALUE_BET_DIAGNOSTIC_KEYS,  # noqa: F401  (re-export for downstream callers)
    attach_shadow_risk_tags as _attach_shadow_risk_tags,
    build_base_candidate_payload as _build_base_candidate_payload,
    compact_state_value_bet_diagnostics as _compact_state_value_bet_diagnostics,
    record_early_skip as _record_early_skip,
)
from signal_pipeline_state_value import (  # noqa: E402
    attach_score_transition_shadow_fields as _attach_score_transition_shadow_fields,
    compute_state_value_snapshot as _compute_state_value_snapshot,
)
from signal_pipeline_no_score_drift import (  # noqa: E402
    maybe_record_no_score_drift_candidate as _maybe_record_no_score_drift_candidate,
)
from inventory_tracker import (  # noqa: E402
    InventorySnapshot, build_inventory_snapshot, load_ledger_rows,
)
from live_quote_engine import (  # noqa: E402
    QuoteDecisionContext, QuoteEngineConfig,
    append_shadow_decision, compute_quote_decision, shadow_ledger_path,
)
from signal_pipeline_capture import (  # noqa: E402
    LATE_STAGE_SKIP_GATES,  # noqa: F401  (re-export for downstream callers)
    attach_trade_features as _attach_trade_features,
    record_skip as _record_skip_impl,
)
from signal_pipeline_gates_pre_fv import (  # noqa: E402
    evaluate_early_gates as _evaluate_early_gates,
    evaluate_pre_inference_gates as _evaluate_pre_inference_gates,
)
from signal_pipeline_gates_post_fv import (  # noqa: E402
    evaluate_post_fv_gates as _evaluate_post_fv_gates,
    run_inference_and_fv_phase as _run_inference_and_fv_phase,
)

LOGGER = logging.getLogger("signal_engine")


# Skip reasons that warrant Family A-E feature capture for fill-model training.
# These fire AFTER the FV model is computed, so candidate_payload has fair_value,
# edge, etc. populated. Earlier gates (gate_min_inning, gate_min_entry_ask) are
# environmental/timing skips with no signal content — excluded to avoid 7,000+
# wasted HTTP calls per session.
#
# Deduplication uses (reason, game_pk, line, inning, inning_state, outs,
# current_total) so we get one feature snapshot per unique game state per gate
# rather than identical snapshots every 2.5s tick.
LATE_STAGE_SKIP_GATES = frozenset({
    "gate_min_edge",
    "gate_fv_saturation",
    "gate_fv_ask_gap",
    "gate_extreme_edge",                  # [TR17] enforced 2026-05-01 (any-inning phantom-run protection)
    "gate_runs_needed_max",
    "gate_runs_pace",
    "gate_close_game_runs_needed",
    "gate_inning5_runs_needed",
    "gate_inning6_runs_needed",
    "gate_blowout",
    "gate_stage2_suppression",
    "gate_sp_era",
})


# STATE_VALUE_BET_DIAGNOSTIC_KEYS, _compact_state_value_bet_diagnostics,
# _attach_shadow_risk_tags, _build_base_candidate_payload, _record_early_skip
# all live in signal_pipeline_payload.py and are re-imported below.


# _now_iso, _now_ts, _runs_pace_ok, _ask_edge_boost live in line_state.py
# and are imported below for use in this module's process_tick orchestrator.


# State-value cluster (lookup_state_value_components,
# apply_state_value_adjustments, compute_state_value_snapshot,
# attach_score_transition_shadow_fields, line_code) lives in
# signal_pipeline_state_value.py and is imported below.


# _maybe_record_no_score_drift_candidate lives in
# signal_pipeline_no_score_drift.py and is imported below.


@dataclass(frozen=True)
class TickContext:
    """Immutable per-tick values shared by gate-phase helpers."""

    game: ScheduledGame
    market: OUMarket
    state: Any
    now: float
    inning: int
    inning_state: str
    away_score: int
    home_score: int
    outs: int
    runners_on: int
    current_total: int
    line_val: float
    best_bid: Optional[float]
    ask: float
    book: Dict[str, Any]
    away_inning_runs: tuple[int, ...] = ()
    home_inning_runs: tuple[int, ...] = ()


@dataclass(frozen=True)
class PreInferenceGateResult:
    """Result from Gates 6-8e before model/FV inference begins."""

    stopped: bool
    runs_needed: Optional[float] = None
    lead: Optional[int] = None
    trailing_runs: Optional[int] = None


@dataclass(frozen=True)
class FvPhaseResult:
    """Computed inference/model state needed by post-FV gates and placement."""

    stopped: bool
    half: str = ""
    batting_is_away: bool = False
    inferred_runs: Optional[int] = None
    base_fair_value: Optional[float] = None
    fair_value: Optional[float] = None
    fair_value_raw: Optional[float] = None
    stage2_run_env_delta: float = 0.0
    team_offense_delta: float = 0.0
    edge: Optional[float] = None
    min_edge_base: Optional[float] = None
    ask_edge_boost: float = 0.0
    min_edge: Optional[float] = None


# _build_base_candidate_payload + _record_early_skip live in
# signal_pipeline_payload.py and are imported below as
# `_build_base_candidate_payload` / `_record_early_skip` for compatibility
# with existing call-sites in this module.


def _get_inventory_snapshot(engine) -> InventorySnapshot:
    """Cache-or-build the inventory snapshot for the quote engine.

    Phase C v1: snapshot is built once per session from the existing
    `live_orders_ledger.jsonl` and reused for all shadow-quote emissions.
    Real placed bets after session start are NOT reflected (they would
    require updating the snapshot on every `_place_bet` call, which is
    a Phase D concern). For shadow output this is approximately correct
    -- the operator sees "what would the engine quote given inventory
    as of session start," which is meaningful for proving the MM idea
    without a real-time accounting layer.
    """
    cached = getattr(engine, "_inventory_snapshot_cache", None)
    if cached is not None:
        return cached
    ledger_path = getattr(engine, "_live_orders_path", None)
    if ledger_path is None:
        # Paper-only engines don't have a ledger path; return empty
        # snapshot so the quote engine can still run.
        snapshot = build_inventory_snapshot([], generated_at_utc=_now_iso())
    else:
        rows = load_ledger_rows(Path(ledger_path))
        snapshot = build_inventory_snapshot(
            rows, generated_at_utc=_now_iso(),
        )
    engine._inventory_snapshot_cache = snapshot
    LOGGER.info(
        "Quote engine: cached inventory snapshot (%d games with positions)",
        len(snapshot.by_game),
    )
    return snapshot


def _maybe_emit_shadow_quote(engine, ctx, *, over_fair_value: float) -> None:
    """Compute a two-sided shadow quote for one tick and append it
    to the per-date shadow ledger. No-op when the quote engine is off.

    Defensive: NEVER raises -- a malformed quote computation must not
    block the OVER pipeline. The shadow ledger is observability only.
    """
    mode = getattr(engine, "_quote_engine_mode", "off")
    if mode != "shadow":
        return
    try:
        cfg = getattr(engine, "_quote_engine_config", None) or QuoteEngineConfig()
        snapshot = _get_inventory_snapshot(engine)
        net_inv = snapshot.net_inventory_for_game(ctx.game.game_pk)
        book = ctx.book if isinstance(ctx.book, dict) else {}
        qctx = QuoteDecisionContext(
            game_pk=ctx.game.game_pk,
            line=ctx.market.line,
            over_best_bid=ctx.best_bid,
            over_best_ask=ctx.ask,
            under_best_bid=book.get("under_best_bid"),
            under_best_ask=book.get("under_best_ask"),
            over_fair_value=over_fair_value,
            under_fair_value=(
                1.0 - over_fair_value if over_fair_value is not None else None
            ),
            under_pair_available=bool(book.get("under_pair_available")),
            net_inventory_over_shares=net_inv,
            config=cfg,
        )
        decision = compute_quote_decision(qctx)
        # Engine surfaces `paper_root` at trade_args.paper_root, but
        # we want the LIVE root specifically for shadow live runs;
        # fall back to paper_root for engines without _live_root.
        root = (
            getattr(engine, "_live_root", None)
            or getattr(engine, "trade_args", None).paper_root
            if getattr(engine, "trade_args", None) is not None
            else Path(".")
        )
        path = shadow_ledger_path(Path(root), engine.date_str)
        append_shadow_decision(path, decision)
    except Exception as exc:  # noqa: BLE001
        # Shadow MUST be fail-open: never break the OVER pipeline.
        LOGGER.warning(
            "Shadow quote-engine emit failed for game_pk=%s: %r "
            "(shadow only -- live trading unaffected)",
            getattr(ctx.game, "game_pk", "?"), exc,
        )


def process_tick(
    self,
    game: ScheduledGame,
    market: OUMarket,
    payload: dict,
    book: dict,
    ask: float,
    line_state_cls,
) -> None:
    key = (game.game_pk, market.line)
    state = self._line_states.setdefault(
        key, line_state_cls(game_pk=game.game_pk, line=market.line)
    )
    now = _now_ts()

    # [TR2 Change 2] Push ask (not mid) for baseline and jump tracking
    state.push_ask(ask)

    # [Family C] Maintain a timestamped (bid, ask, mid, spread) buffer for
    # velocity / drift features. Skipped silently if best_bid is missing.
    state.push_tick(ts=now, bid=book.get("best_bid"), ask=ask)

    if state.bet_open:
        return

    # Extract game state early â€” needed for several TR3 filters
    inning = payload.get("inning")
    if inning is None:
        return

    inning_state = str(payload.get("inning_state") or "")
    away_score = payload.get("away_score")
    home_score = payload.get("home_score")
    outs = payload.get("outs")
    runners_on = int(payload.get("runners_on") or 0)

    if None in (away_score, home_score, outs):
        return

    update_score_segment = getattr(state, "update_score_segment", None)
    if callable(update_score_segment):
        update_score_segment(
            away_score=away_score,
            home_score=home_score,
            ask=ask,
            now=now,
        )

    if state.baseline_ask is None:
        return

    current_total = away_score + home_score
    line_val = float(market.line)
    best_bid = book.get("best_bid")

    def _inning_runs(raw: object) -> tuple[int, ...]:
        if not isinstance(raw, (list, tuple)):
            return ()
        out = []
        for value in raw:
            try:
                out.append(max(0, int(float(value))))
            except (TypeError, ValueError):
                out.append(0)
        return tuple(out)

    ctx = TickContext(
        game=game,
        market=market,
        state=state,
        now=now,
        inning=inning,
        inning_state=inning_state,
        away_score=away_score,
        home_score=home_score,
        outs=outs,
        runners_on=runners_on,
        current_total=current_total,
        line_val=line_val,
        best_bid=best_bid,
        ask=ask,
        book=book,
        away_inning_runs=_inning_runs(payload.get("away_inning_runs")),
        home_inning_runs=_inning_runs(payload.get("home_inning_runs")),
    )

    _maybe_record_no_score_drift_candidate(self, ctx)

    if _evaluate_early_gates(self, ctx):
        return

    candidate_payload: Dict[str, object] = _build_base_candidate_payload(self, ctx)

    # Thin closure binding the free record_skip helper to this tick's
    # (engine, ctx, candidate_payload). Gate phase functions accept this as
    # a callable parameter so their internals stay reason-only.
    def _record_skip(
        reason: str,
        shadow_values: Optional[Dict[str, object]] = None,
        secondary_shadow_reason: Optional[str] = None,
        secondary_shadow_values: Optional[Dict[str, object]] = None,
        extra_fields: Optional[Dict[str, object]] = None,
    ) -> None:
        _record_skip_impl(
            self, ctx, candidate_payload, reason,
            shadow_values=shadow_values,
            secondary_shadow_reason=secondary_shadow_reason,
            secondary_shadow_values=secondary_shadow_values,
            extra_fields=extra_fields,
        )

    pre_inference_gate = _evaluate_pre_inference_gates(
        self,
        ctx,
        candidate_payload,
        _record_skip,
    )
    if pre_inference_gate.stopped:
        return
    runs_needed = float(pre_inference_gate.runs_needed)
    lead = int(pre_inference_gate.lead)
    trailing_runs = int(pre_inference_gate.trailing_runs)

    fv_phase = _run_inference_and_fv_phase(
        self,
        ctx,
        candidate_payload,
        _record_skip,
    )
    if fv_phase.stopped:
        return
    # Phase C shadow (2026-05-17): emit a two-sided quote decision
    # for THIS tick before the late-stage gates run, so the shadow
    # ledger sees the same coverage regardless of whether the OVER
    # late gates accept or skip. No-op when --quote-engine-mode is
    # `off` (the default).
    _maybe_emit_shadow_quote(
        self, ctx, over_fair_value=float(fv_phase.fair_value),
    )
    if _evaluate_post_fv_gates(self, ctx, candidate_payload, fv_phase, _record_skip):
        return

    batting_is_away = fv_phase.batting_is_away
    best_run_count = int(fv_phase.inferred_runs)
    base_fv = float(fv_phase.base_fair_value)
    best_fv = float(fv_phase.fair_value)
    stage2_run_env_delta = float(fv_phase.stage2_run_env_delta)
    team_offense_delta = float(fv_phase.team_offense_delta)
    edge = float(fv_phase.edge)
    line_inning_key = (game.game_pk, market.line)
    state_value_diagnostics = _compact_state_value_bet_diagnostics(candidate_payload)

    ltp_at_signal = book.get("ltp")   # last traded price; gap to ask = adversarial-selection indicator
    placed_bet = self._place_bet(
        game=game,
        market=market,
        best_ask=ask,
        fair_value=best_fv,
        base_fair_value=base_fv,
        stage2_run_env_delta=stage2_run_env_delta,
        team_offense_delta=team_offense_delta,
        edge=edge,
        inferred_runs=best_run_count,
        inning=inning,
        inning_state=inning_state,
        outs=outs,
        away_score_before=away_score,
        home_score_before=home_score,
        batting_is_away=batting_is_away,
        runners_on=runners_on,
        decision_bid=best_bid,
        ltp=ltp_at_signal,
        state_value_diagnostics=state_value_diagnostics,
    )
    if placed_bet is None:
        skip_reason = str(getattr(self, "_last_place_bet_skip_reason", "") or "").strip()
        _record_skip(skip_reason or "place_bet_rejected")
        if hasattr(self, "_last_place_bet_skip_reason"):
            self._last_place_bet_skip_reason = None
        return

    candidate_trade_payload = dict(candidate_payload)
    _attach_trade_features(
        self,
        ctx,
        placed_bet=placed_bet,
        candidate_trade_payload=candidate_trade_payload,
        fv_phase=fv_phase,
        runs_needed=runs_needed,
        lead=lead,
        trailing_runs=trailing_runs,
    )
    self._record_candidate_decision(candidate_trade_payload)

    self._last_bet_ts[game.game_pk] = now
    self._last_bet_edge[game.game_pk] = edge
    self._last_bet_inning[line_inning_key] = inning
    self._last_bet_edge_by_line[line_inning_key] = edge
    state.reset_after_bet()

    # --- Post-signal book capture (limit order calibration) ---
    # Captures bid/ask/ltp at 1-second intervals for 10 seconds after the signal.
    # Data is written to data/paper_trading/book_captures/{date}/{bet_id}.jsonl
    # and used to calibrate limit order pricing per buying_model_1.txt.
    self._start_book_capture(
        bet=placed_bet,
        token_id=market.over_token_id,
        initial_book=book,
        signal_ts=now,
    )
