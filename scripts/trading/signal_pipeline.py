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
from models import BetRecord, signal_context_fields  # noqa: E402

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


def _place_under_paper_bet(
    *,
    engine,
    ctx,
    over_fv_phase: "FvPhaseResult",
    over_candidate_payload: Dict[str, object],
    under_fv: float,
    under_fv_raw: float,
    under_ask: float,
    under_edge: float,
) -> Optional[BetRecord]:
    """Phase C-paper (2026-05-27): record a paper BetRecord(side="under").

    Mirrors the inferred-state derivation that SignalEngine._place_bet
    uses for OVER, but the resulting bet is plain paper (never sent to
    the CLOB) and the live-engine placement path is explicitly guarded
    against side=under for defense-in-depth. UNDER capture sidecars
    (book/tape/velocity) are intentionally not started here -- those
    pipelines are OVER-bet-focused; UNDER paper bets only need the
    BetRecord to flow through the settlement loop.
    """
    game = ctx.game
    market = ctx.market

    engine._under_bet_counter = (
        getattr(engine, "_under_bet_counter", 0) + 1
    )
    bet_id = (
        f"{engine.date_str}_{game.game_pk}_{market.line}"
        f"_under_{engine._under_bet_counter:04d}"
    )

    def _payload_int(name: str, default: int = 0) -> int:
        v = over_candidate_payload.get(name)
        if v is None or v == "":
            return default
        try:
            return int(float(v))
        except (TypeError, ValueError):
            return default

    inferred_runs = _payload_int("inferred_runs", 0)
    inning = _payload_int("inning", 1)
    inning_state = str(over_candidate_payload.get("inning_state") or "")
    outs = _payload_int("outs", 0)
    away_before = _payload_int("away_score_before", 0)
    home_before = _payload_int("home_score_before", 0)
    runners_on = _payload_int("runners_on", 0)
    batting_is_away = inning_state.upper().startswith("T")
    inf_away = (
        away_before + inferred_runs if batting_is_away else away_before
    )
    inf_home = (
        home_before if batting_is_away else home_before + inferred_runs
    )

    try:
        stake = float(engine.trade_args.stake)
    except (TypeError, ValueError):
        stake = 100.0

    bet = BetRecord(
        bet_id=bet_id,
        placed_at=_now_iso(),
        game_pk=game.game_pk,
        away_abbrev=game.away_abbrev,
        home_abbrev=game.home_abbrev,
        line=str(market.line),
        side="under",
        entry_ask=under_ask,
        fair_value=under_fv,
        base_fair_value=float(1.0 - float(over_fv_phase.base_fair_value)),
        stage2_run_env_delta=round(
            -1.0 * float(over_fv_phase.stage2_run_env_delta), 4
        ),
        team_offense_delta=round(
            -1.0 * float(over_fv_phase.team_offense_delta), 4
        ),
        edge=under_edge,
        inferred_runs=inferred_runs,
        inning=inning,
        inning_state=inning_state,
        outs=outs,
        away_score_before=away_before,
        home_score_before=home_before,
        inferred_away_after=inf_away,
        inferred_home_after=inf_home,
        stake=stake,
        runners_on=runners_on,
        venue_name=getattr(game, "venue_name", "") or "",
        config_label=str(
            getattr(engine.trade_args, "config_label", "default") or "default"
        ),
        **signal_context_fields(game),
    )
    engine._bets.append(bet)

    half = inning_state[:1].upper() if inning_state else "?"
    batting_team = (
        game.away_abbrev if half == "T" else game.home_abbrev
    )
    current_total = away_before + home_before
    LOGGER.info(
        "PAPER UNDER BET [%s] %s@%s O/U %s UNDER @ %.3f  FV=%.3f  "
        "edge=%.3f  inn=%s%d outs=%d  %s inferred +%d runs  "
        "score=%d-%d(total=%d)  stake=$%.0f",
        bet_id,
        game.away_abbrev, game.home_abbrev, market.line,
        under_ask, under_fv, under_edge,
        half, inning, outs,
        batting_team, inferred_runs,
        away_before, home_before, current_total, stake,
    )

    save_session = getattr(engine, "_save_session", None)
    if callable(save_session):
        try:
            save_session()
        except Exception as exc:  # noqa: BLE001
            LOGGER.debug(
                "UNDER paper bet save_session failed: %r", exc
            )
    return bet


def _maybe_emit_under_candidate(
    engine,
    ctx,
    over_fv_phase: "FvPhaseResult",
    over_candidate_payload: Dict[str, object],
) -> None:
    """Phase A5 (2026-05-19) -> Phase C-paper (2026-05-27): emit an
    UNDER candidate row alongside OVER and, in paper mode, place a
    paper UNDER BetRecord.

    No-op when `--under-mode off` (default). When `shadow`, builds a
    sibling UNDER candidate row whose `fair_value` is
    `(1 - over_fv_raw)` passed through the UNDER calibrator, gets the
    UNDER ask from the market book, evaluates the 5 symmetric UNDER
    gates + gate_min_edge, and appends to the candidate log via the
    engine's standard `_record_candidate_decision` path. NO UNDER bets
    placed. When `paper`, runs the same gates AND, when all pass,
    appends a BetRecord(side="under") to engine._bets so the standard
    settlement loop picks it up.

    Fail-open: any exception is logged at DEBUG and swallowed; OVER's
    downstream flow is unaffected.
    """
    mode = getattr(engine, "_under_mode", None) or getattr(
        engine, "_under_emission_mode", "off"
    )
    if mode not in {"shadow", "paper", "live"}:
        return
    try:
        from model_families import SCORE_EVENT_TRANSITION  # noqa: WPS433

        game = ctx.game
        market = ctx.market
        book = ctx.book if isinstance(ctx.book, dict) else {}
        over_fv_raw = over_fv_phase.fair_value_raw
        if over_fv_raw is None:
            return
        try:
            over_fv_raw_f = float(over_fv_raw)
        except (TypeError, ValueError):
            return
        # UNDER raw = 1 - OVER raw (pre-calibration). Apply the UNDER
        # calibrator based on under_calibration_mode:
        #   enforce: calibrated value is used as under_fv (when calibrator loaded).
        #   shadow:  calibrated value is computed + logged but raw is used.
        #   off:     calibrator is not loaded; under_fv = under_fv_raw.
        under_fv_raw = 1.0 - over_fv_raw_f
        under_cal = getattr(engine, "_under_prob_calibrator", None)
        under_cal_mode = getattr(engine, "_under_calibration_mode", "enforce")
        under_fv_calibrated_shadow: Optional[float] = None
        if under_cal is not None:
            try:
                under_fv_calibrated_shadow = float(under_cal.calibrate(
                    under_fv_raw,
                    model_family=SCORE_EVENT_TRANSITION,
                ))
            except Exception:  # noqa: BLE001
                under_fv_calibrated_shadow = None
        if under_cal_mode == "enforce" and under_fv_calibrated_shadow is not None:
            under_fv = under_fv_calibrated_shadow
        else:
            # shadow + off + (enforce with calibrator load failure)
            under_fv = under_fv_raw

        under_best_ask = book.get("under_best_ask")
        under_best_bid = book.get("under_best_bid")
        under_pair_available = bool(book.get("under_pair_available"))

        # Build the UNDER candidate payload by COPYING the OVER row's
        # market context (game, line, inning, state, etc.) and
        # overwriting the side-specific fields. This keeps every
        # diagnostic the OVER row carries (state_value, weather,
        # stage1 support metadata, etc.) visible on the UNDER row
        # too, so downstream consumers don't need to special-case
        # UNDER.
        under_payload = dict(over_candidate_payload)
        under_payload["side"] = "under"
        # 2026-05-23 (audit followup): mark every UNDER candidate row
        # as calibration-unreliable until the UNDER calibrator is
        # refit. The 5/22 DET@BAL O7.5 debut showed model P(under)=
        # 0.73 vs actual final=11 runs -- 17 emitted rows would have
        # been a -$170 day if live. Downstream cohort/calibration
        # health blocks should filter on this field until a refit
        # changes the status to "verified" or similar.
        under_payload["shadow_under_calibration_status"] = "unreliable_pre_refit"
        # bet_id mirrors OVER's with a `_under_shadow` suffix so any
        # cross-side joins (downstream training table merge logic)
        # can pair them.
        over_bet_id = str(over_candidate_payload.get("bet_id") or "")
        under_payload["bet_id"] = (
            f"{over_bet_id}_under_shadow" if over_bet_id
            else f"{game.game_pk}_{market.line}_under_shadow"
        )
        under_payload["over_bet_id"] = over_bet_id or None
        under_payload["entry_ask"] = under_best_ask
        under_payload["best_bid"] = under_best_bid
        under_payload["decision_best_bid"] = under_best_bid
        under_payload["decision_best_ask"] = under_best_ask
        # decision_ask defaults to OVER's value in the dict copy
        # above; overwrite with UNDER's ask so cohort-bucketing-by-ask
        # downstream looks at the right side. (Caught during the
        # 2026-05-19 under-outcomes counterfactual block design.)
        under_payload["decision_ask"] = under_best_ask
        under_payload["under_pair_available"] = under_pair_available

        # UNDER FV chain (mirrors OVER but on the complement side).
        under_payload["fair_value_raw"] = under_fv_raw
        under_payload["fair_value"] = under_fv
        # 2026-05-30: record which calibration mode produced this FV.
        # shadow + off both leave fair_value = fair_value_raw, but the
        # mode tag lets downstream analysis bucket the two cleanly.
        under_payload["under_calibration_mode"] = under_cal_mode
        if under_fv_calibrated_shadow is not None:
            under_payload["fair_value_under_calibrated_shadow"] = (
                under_fv_calibrated_shadow
            )
        under_payload["base_fair_value"] = 1.0 - float(
            over_fv_phase.base_fair_value
        )
        # Stage-2/3 deltas are SYMMETRIC by construction (logit
        # additive on the over probability; equivalent magnitude
        # opposite sign on the under probability). Carry the signed
        # value so cohort analysis can compare side-by-side.
        under_payload["stage2_run_env_delta"] = -1.0 * float(
            over_fv_phase.stage2_run_env_delta
        )
        under_payload["team_offense_delta"] = -1.0 * float(
            over_fv_phase.team_offense_delta
        )

        if under_best_ask is None:
            # No UNDER liquidity at this tick. Record the candidate
            # row as a skip with reason; the operator can see UNDER
            # coverage rate from these skips.
            under_payload["edge"] = None
            under_payload["decision"] = "skip"
            under_payload["decision_reason"] = "gate_no_under_liquidity"
            engine._record_candidate_decision(under_payload)
            return

        try:
            under_ask_f = float(under_best_ask)
        except (TypeError, ValueError):
            return
        under_edge = under_fv - under_ask_f
        under_payload["edge"] = under_edge

        try:
            min_edge = float(
                getattr(engine.trade_args, "edge_threshold", 0.10)
            )
        except (TypeError, ValueError):
            min_edge = 0.10

        # Phase C-paper (2026-05-27): 5 symmetric UNDER gates +
        # gate_min_edge. Asymmetric OVER gates (pace, runs_needed,
        # close_game, inn5/6 dead zone, blowout, S2 suppress, pitcher
        # boost) are intentionally NOT mirrored -- they work in the
        # opposite direction for UNDER and need explicit UNDER-
        # specific design after paper data accumulates.
        try:
            inning_int = int(
                getattr(ctx, "inning", None)
                if getattr(ctx, "inning", None) is not None
                else (over_candidate_payload.get("inning") or 0)
            )
        except (TypeError, ValueError):
            inning_int = 0
        try:
            line_f = float(market.line)
        except (TypeError, ValueError):
            line_f = 0.0
        high_line_cutoff = float(getattr(
            engine.trade_args, "high_line_cutoff", 8.5,
        ))
        is_high_line = line_f >= high_line_cutoff
        under_base_fv = float(under_payload["base_fair_value"])

        under_skip_reason: Optional[str] = None
        under_skip_values: Dict[str, object] = {}

        # Gate U1: min_inning (symmetric — variance reduction)
        min_inning_attr = (
            "under_min_inning_high_line" if is_high_line
            else "under_min_inning"
        )
        min_inning_thresh = int(getattr(
            engine.trade_args, min_inning_attr,
            5 if is_high_line else 4,
        ))
        if inning_int < min_inning_thresh:
            under_skip_reason = "gate_under_min_inning"
            under_skip_values = {
                "inning": inning_int,
                "min_inning": min_inning_thresh,
            }

        # Gate U2: min_entry_ask on UNDER side (thin-book guard).
        if under_skip_reason is None:
            min_ask_attr = (
                "under_min_entry_ask_high_line" if is_high_line
                else "under_min_entry_ask"
            )
            min_ask_thresh = float(getattr(
                engine.trade_args, min_ask_attr,
                0.60 if is_high_line else 0.55,
            ))
            if under_ask_f < min_ask_thresh:
                under_skip_reason = "gate_under_min_entry_ask"
                under_skip_values = {
                    "under_ask": under_ask_f,
                    "min_ask": min_ask_thresh,
                }

        # Gate U3: max_base_fv (FV saturation / phantom no-score).
        if under_skip_reason is None:
            max_base_fv_thresh = float(getattr(
                engine.trade_args, "under_max_base_fv", 0.99,
            ))
            if under_base_fv >= max_base_fv_thresh:
                under_skip_reason = "gate_under_max_base_fv"
                under_skip_values = {
                    "under_base_fv": under_base_fv,
                    "max_base_fv": max_base_fv_thresh,
                }

        # Gate U4: extreme_edge (symmetric TR19 protection).
        if under_skip_reason is None:
            extreme_edge_max = float(getattr(
                engine.trade_args, "under_extreme_edge_max", 0.22,
            ))
            if under_edge > extreme_edge_max:
                under_skip_reason = "gate_under_extreme_edge"
                under_skip_values = {
                    "under_edge": under_edge,
                    "extreme_edge_max": extreme_edge_max,
                }

        # Gate U5: fv_ask_gap (late-inning large gap = market
        # disagreement signal).
        if under_skip_reason is None:
            gap_max = float(getattr(
                engine.trade_args, "under_fv_ask_gap_max", 0.26,
            ))
            gap_min_inning = int(getattr(
                engine.trade_args, "under_fv_ask_gap_min_inning", 7,
            ))
            if under_edge > gap_max and inning_int >= gap_min_inning:
                under_skip_reason = "gate_under_fv_ask_gap"
                under_skip_values = {
                    "under_edge": under_edge,
                    "gap_max": gap_max,
                    "inning": inning_int,
                }

        # gate_min_edge (existing, runs last so the 5 structural
        # gates fire first on cohort audits).
        if under_skip_reason is None and under_edge < min_edge:
            under_skip_reason = "gate_min_edge"
            under_skip_values = {
                "under_edge": under_edge,
                "min_edge": min_edge,
            }

        if under_skip_reason is not None:
            under_payload["decision"] = "skip"
            under_payload["decision_reason"] = under_skip_reason
            for k, v in under_skip_values.items():
                under_payload.setdefault(f"under_gate_{k}", v)
            engine._record_candidate_decision(under_payload)
            return

        # 2026-06-03 fix: UNDER-side dedup. Mirrors OVER's Gate 9 (same-
        # event 60s window) + Gate 10 (cross-inning same-line) but on the
        # parallel _last_under_bet_* dicts, so an OVER bet on the same
        # (game, line) does not block UNDER and vice versa. Without this,
        # M_under_paper fired 5 paper UNDER bets on TEX@STL 10.5 in 17
        # seconds on 2026-06-02 (all lost). Reuses OVER's thresholds
        # (event_dedup_secs, inning_dedup_gap, inning_dedup_edge_gap)
        # because the same temporal logic applies regardless of side.
        # Uses getattr-with-default lookups so duck-typed test engines
        # (and any external callers built before this fix) don't need
        # to know about the new attributes.
        now_ts = _now_ts()
        line_inning_key_under = (game.game_pk, market.line)
        event_dedup_secs = float(getattr(
            engine.trade_args, "event_dedup_secs", 60.0,
        ))
        inning_dedup_gap = int(getattr(
            engine.trade_args, "inning_dedup_gap", 3,
        ))
        inning_dedup_edge_gap = float(getattr(
            engine.trade_args, "inning_dedup_edge_gap", 0.02,
        ))
        # Resolve dedup dicts via getattr with a None sentinel + lazy
        # init on the engine. The `or {}` shortcut would mis-fire here
        # because an empty dict is falsy and would alias to a fresh
        # dict, severing the write-back to the engine.
        def _ensure_dict(attr: str) -> Dict:
            d = getattr(engine, attr, None)
            if d is None:
                d = {}
                setattr(engine, attr, d)
            return d
        last_under_ts_dict = _ensure_dict("_last_under_bet_ts")
        last_under_edge_dict = _ensure_dict("_last_under_bet_edge")
        last_under_inning_dict = _ensure_dict("_last_under_bet_inning")
        last_under_edge_by_line_dict = _ensure_dict(
            "_last_under_bet_edge_by_line",
        )
        under_dedup_skip_reason: Optional[str] = None
        last_under_ts = last_under_ts_dict.get(game.game_pk, 0.0)
        if (now_ts - last_under_ts) < event_dedup_secs:
            last_under_edge = last_under_edge_dict.get(game.game_pk, 0.0)
            if under_edge <= last_under_edge:
                under_dedup_skip_reason = "gate_under_event_dedup"
        if under_dedup_skip_reason is None:
            last_under_inning = last_under_inning_dict.get(
                line_inning_key_under, -1,
            )
            if last_under_inning >= 0:
                innings_elapsed = inning_int - last_under_inning
                if innings_elapsed < inning_dedup_gap:
                    last_line_edge = last_under_edge_by_line_dict.get(
                        line_inning_key_under, 0.0,
                    )
                    if (under_edge - last_line_edge) <= inning_dedup_edge_gap:
                        under_dedup_skip_reason = "gate_under_inning_dedup"
        if under_dedup_skip_reason is not None:
            under_payload["decision"] = "skip"
            under_payload["decision_reason"] = under_dedup_skip_reason
            engine._record_candidate_decision(under_payload)
            return

        # All UNDER gates passed. Decision tag distinguishes shadow
        # (no bet) from paper/live (BetRecord recorded / order placed).
        if mode == "paper":
            under_payload["decision"] = "paper_under"
            under_payload["decision_reason"] = "paper_under_gates_pass"
        elif mode == "live":
            under_payload["decision"] = "live_under"
            under_payload["decision_reason"] = "live_under_gates_pass"
        else:
            under_payload["decision"] = "shadow_under"
            under_payload["decision_reason"] = "shadow_under_gates_pass"
        engine._record_candidate_decision(under_payload)

        if mode in ("paper", "live"):
            try:
                # Polymorphic: SignalEngine._place_under_bet -> paper
                # BetRecord; LiveTradingEngine._place_under_bet -> real
                # CLOB order on the under_no token when mode == "live".
                placer = getattr(engine, "_place_under_bet", None)
                if callable(placer):
                    placed = placer(
                        ctx=ctx,
                        over_fv_phase=over_fv_phase,
                        over_candidate_payload=over_candidate_payload,
                        under_fv=under_fv,
                        under_fv_raw=under_fv_raw,
                        under_ask=under_ask_f,
                        under_edge=under_edge,
                    )
                else:  # back-compat: older engines without the method
                    placed = _place_under_paper_bet(
                        engine=engine,
                        ctx=ctx,
                        over_fv_phase=over_fv_phase,
                        over_candidate_payload=over_candidate_payload,
                        under_fv=under_fv,
                        under_fv_raw=under_fv_raw,
                        under_ask=under_ask_f,
                        under_edge=under_edge,
                    )
                # Update UNDER dedup state only on successful placement so
                # a rejected/skipped placer call leaves the window open for
                # the next genuine signal (parity with OVER's behavior:
                # _last_bet_* writes are gated on a non-None _place_bet
                # return upstream). Tolerates fake engines that don't carry
                # the dedup dicts by mutating the dict objects we already
                # resolved above (real SignalEngine + LiveTradingEngine
                # share the same dicts via their __init__).
                if placed is not None:
                    last_under_ts_dict[game.game_pk] = now_ts
                    last_under_edge_dict[game.game_pk] = under_edge
                    last_under_inning_dict[line_inning_key_under] = (
                        inning_int
                    )
                    last_under_edge_by_line_dict[
                        line_inning_key_under
                    ] = under_edge
            except Exception as exc:  # noqa: BLE001
                LOGGER.warning(
                    "UNDER %s bet failed for game_pk=%s line=%s: %r "
                    "(candidate row already recorded; OVER pipeline "
                    "unaffected).",
                    mode, game.game_pk, market.line, exc,
                )
    except Exception as exc:  # noqa: BLE001
        LOGGER.debug(
            "UNDER candidate emit failed for game_pk=%s line=%s: %r "
            "(shadow only -- OVER pipeline unaffected)",
            getattr(getattr(ctx, "game", None), "game_pk", "?"),
            getattr(getattr(ctx, "market", None), "line", "?"),
            exc,
        )


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
    # Phase A5 (2026-05-19): emit the sibling UNDER candidate AT
    # THIS POINT, before OVER's late-stage gates. UNDER's gate
    # evaluation is independent of OVER's, so we want UNDER
    # candidate coverage that is NOT correlated with OVER's
    # downstream gate filtering. No-op when
    # --under-emission-mode off (the default).
    _maybe_emit_under_candidate(
        self, ctx, fv_phase, candidate_payload,
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
