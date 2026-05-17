#!/usr/bin/env python3
"""
signal_pipeline_state_value.py -- Shadow current-score FV diagnostics.

Free functions extracted from signal_pipeline.py (Tier 4 refactor, 2026-05-01).
All purely diagnostic: compute the current-state Poisson + empirical FVs
(without inferring the score-event runs), apply the same Stage-2/3
adjustments, and decorate candidate rows with phantom-risk score and
score-event-vs-no-event proxies. None of this code blocks or places trades;
it exists so post-session analysis can audit whether weak current-state
support correlates with losses.

Surfaces:
  - line_code(line) -> Optional[str]
  - lookup_state_value_components(engine, *, away_score, home_score, inning,
                                  inning_state, outs, line, runners_on)
  - apply_state_value_adjustments(engine, *, ctx, base_prob)
  - compute_state_value_snapshot(engine, ctx, *, away_score=None, home_score=None)
  - attach_score_transition_shadow_fields(*, trade_args, ctx, state_snapshot,
                                          candidate_payload, after_event_fv,
                                          inferred_runs)

Engine attrs read:
  - engine.cache  (with .lookup or .lookup_with_meta + .cells)
  - engine.stage2_model, engine.offense_model
"""

from __future__ import annotations

import math
from typing import Any, Dict, Optional, TYPE_CHECKING

from stage1_cache_audit import resolve_cell_line_probability
from stage1_support import prefixed_stage1_support_fields
from stage2_run_env_model import RunEnvContext
from signal_config import DEFAULT_JUMP_THRESHOLD, DEFAULT_LOOKBACK
from weather_client import weather_v2_run_env_game_data, weather_v2_source_label

if TYPE_CHECKING:
    from signal_engine import SignalEngine
    from signal_pipeline import TickContext


def line_code(line: str) -> Optional[str]:
    try:
        return str(int(round(float(line) * 10)))
    except Exception:
        return None


def lookup_state_value_components(
    engine: "SignalEngine",
    *,
    away_score: int,
    home_score: int,
    inning: int,
    inning_state: str,
    outs: int,
    line: str,
    runners_on: int,
) -> Optional[Dict[str, object]]:
    """Return current-state Poisson/empirical cache values plus lookup metadata."""
    cache = getattr(engine, "cache", None)
    if cache is None:
        return None

    lookup_meta = None
    base_poisson = None
    lookup_with_meta = getattr(cache, "lookup_with_meta", None)
    try:
        if callable(lookup_with_meta):
            base_poisson, lookup_meta = lookup_with_meta(
                away_score=away_score,
                home_score=home_score,
                inning=inning,
                inning_state=inning_state,
                outs=outs,
                line=line,
                runners_on=runners_on,
            )
        else:
            base_poisson = cache.lookup(
                away_score=away_score,
                home_score=home_score,
                inning=inning,
                inning_state=inning_state,
                outs=outs,
                line=line,
                runners_on=runners_on,
            )
    except Exception as exc:
        return {"state_value_lookup_error": str(exc)}

    if base_poisson is None:
        return None

    base_empirical = None
    empirical_line_meta: Dict[str, object] = {}
    code = line_code(line)
    state_cell_key = lookup_meta.get("state_cell_key") if isinstance(lookup_meta, dict) else None
    cells = getattr(cache, "cells", None)
    cell = None
    if code and state_cell_key and isinstance(cells, dict):
        cell = cells.get(state_cell_key)
        if isinstance(cell, dict):
            base_empirical, empirical_line_meta = resolve_cell_line_probability(
                cell,
                requested_line=line,
                prefix="o",
            )

    out: Dict[str, object] = {
        "state_value_base_poisson": float(base_poisson),
        "state_value_base_empirical": base_empirical,
        "state_value_line_key_poisson": f"po{code}" if code else None,
        "state_value_line_key_empirical": f"o{code}" if code else None,
        "state_value_empirical_line_fallback_mode": empirical_line_meta.get("line_fallback_mode"),
        "state_value_empirical_line_source_key": empirical_line_meta.get("line_source_key"),
    }
    if isinstance(lookup_meta, dict):
        out.update(
            {
                "state_value_used_fallback": bool(lookup_meta.get("used_fallback")),
                "state_value_state_fallback_level": lookup_meta.get("state_fallback_level"),
                "state_value_state_fallback_label": lookup_meta.get("state_fallback_label"),
                "state_value_state_cell_key": lookup_meta.get("state_cell_key"),
                "state_value_line_fallback_mode": lookup_meta.get("line_fallback_mode"),
                "state_value_line_source_key": lookup_meta.get("line_source_key"),
            }
        )
    out.update(
        prefixed_stage1_support_fields(
            prefix="state_value",
            cell=cell if isinstance(cell, dict) else None,
            state_fallback_level=out.get("state_value_state_fallback_level"),
            poisson_line_fallback_mode=out.get("state_value_line_fallback_mode"),
            empirical_line_fallback_mode=out.get("state_value_empirical_line_fallback_mode"),
        )
    )
    return out


def apply_state_value_adjustments(
    engine: "SignalEngine",
    *,
    ctx: "TickContext",
    base_prob: float,
) -> Dict[str, float]:
    """Apply the same Stage-2/3 adjustments to a non-inferred current state."""
    adjusted = float(base_prob)
    stage2_delta = 0.0
    stage2_weather_source = None
    stage2_model = getattr(engine, "stage2_model", None)
    if stage2_model is not None:
        try:
            weather_features = {}
            weather_getter = getattr(engine, "_weather_fields_for_game", None)
            if callable(weather_getter):
                weather_features = weather_getter(ctx.game.game_pk)
            stage2_weather_source = weather_v2_source_label(weather_features)
            env_context = RunEnvContext.from_game_data(
                weather_v2_run_env_game_data(
                    weather_features,
                    venue_name=ctx.game.venue_name,
                    game_date_utc=ctx.game.game_date,
                )
            )
            stage2_adjusted = stage2_model.adjust_line(
                line=ctx.market.line,
                base_prob=adjusted,
                context=env_context,
            )
            _eps = 1e-6
            _p0 = max(_eps, min(1.0 - _eps, adjusted))
            _p1 = max(_eps, min(1.0 - _eps, stage2_adjusted))
            stage2_delta = math.log(_p1 / (1.0 - _p1)) - math.log(_p0 / (1.0 - _p0))
            adjusted = stage2_adjusted
        except Exception:
            stage2_delta = 0.0

    team_delta = 0.0
    offense_model = getattr(engine, "offense_model", None)
    if offense_model is not None:
        try:
            game_date = ctx.game.game_date[:10]
            adjusted = offense_model.adjust_fv(
                base_fv=adjusted,
                away_abbrev=ctx.game.away_abbrev,
                home_abbrev=ctx.game.home_abbrev,
                game_date=game_date,
                inning=ctx.inning,
            )
            team_delta = offense_model.get_matchup_delta(
                ctx.game.away_abbrev, ctx.game.home_abbrev, game_date, ctx.inning
            )
        except Exception:
            team_delta = 0.0

    return {
        "state_value_fv_raw": float(adjusted),
        "state_value_stage2_run_env_delta": float(stage2_delta),
        "state_value_stage2_weather_source": stage2_weather_source,
        "state_value_team_offense_delta": float(team_delta),
    }


def compute_state_value_snapshot(
    engine: "SignalEngine",
    ctx: "TickContext",
    *,
    away_score: Optional[int] = None,
    home_score: Optional[int] = None,
) -> Optional[Dict[str, object]]:
    """Compute shadow current-score FV diagnostics without changing decisions."""
    away = ctx.away_score if away_score is None else int(away_score)
    home = ctx.home_score if home_score is None else int(home_score)
    components = lookup_state_value_components(
        engine,
        away_score=away,
        home_score=home,
        inning=ctx.inning,
        inning_state=ctx.inning_state,
        outs=ctx.outs,
        line=ctx.market.line,
        runners_on=ctx.runners_on,
    )
    if not components or components.get("state_value_base_poisson") is None:
        return components

    adjusted = apply_state_value_adjustments(
        engine,
        ctx=ctx,
        base_prob=float(components["state_value_base_poisson"]),
    )
    out = dict(components)
    out.update(adjusted)
    out["state_value_edge"] = out["state_value_fv_raw"] - ctx.ask
    base_empirical = out.get("state_value_base_empirical")
    out["state_value_empirical_edge"] = (
        float(base_empirical) - ctx.ask if base_empirical is not None else None
    )
    out["state_value_away_score"] = away
    out["state_value_home_score"] = home
    out["state_value_total"] = away + home
    return out


def attach_score_transition_shadow_fields(
    *,
    trade_args: Any,
    ctx: "TickContext",
    state_snapshot: Optional[Dict[str, object]],
    candidate_payload: Dict[str, object],
    after_event_fv: float,
    inferred_runs: int,
) -> None:
    """Attach non-enforcing diagnostics for score vs no-score transition risk."""
    if state_snapshot:
        for key, value in state_snapshot.items():
            candidate_payload[f"current_{key}"] = value

    current_fv = None
    if state_snapshot:
        current_fv = state_snapshot.get("state_value_fv_raw")
    try:
        current_fv_float = float(current_fv) if current_fv is not None else None
    except (TypeError, ValueError):
        current_fv_float = None

    no_event_edge = (current_fv_float - ctx.ask) if current_fv_float is not None else None
    after_event_edge = float(after_event_fv) - ctx.ask
    lookback_ticks = int(getattr(trade_args, "lookback_ticks", DEFAULT_LOOKBACK))
    try:
        ask_jump = ctx.state.ask_jump(lookback_ticks)
    except Exception:
        ask_jump = None
    baseline_ask = getattr(ctx.state, "baseline_ask", None)
    baseline_jump = (ctx.ask - baseline_ask) if baseline_ask is not None else None

    try:
        jump_threshold = float(getattr(trade_args, "jump_threshold", DEFAULT_JUMP_THRESHOLD))
    except Exception:
        jump_threshold = DEFAULT_JUMP_THRESHOLD
    if ask_jump is not None:
        jump_threshold = max(jump_threshold, 1e-6)
        score_event_proxy = min(max((float(ask_jump) - jump_threshold) / 0.20, 0.0), 1.0)
    elif baseline_jump is not None:
        score_event_proxy = min(max((float(baseline_jump) - jump_threshold) / 0.20, 0.0), 1.0)
    else:
        score_event_proxy = None

    fv_lift = (
        float(after_event_fv) - current_fv_float
        if current_fv_float is not None
        else None
    )
    phantom_risk_score = 0.0
    if fv_lift is not None:
        phantom_risk_score += max(0.0, min(1.0, fv_lift / 0.35)) * 0.55
    if no_event_edge is not None and no_event_edge < 0:
        phantom_risk_score += min(abs(no_event_edge) / 0.20, 1.0) * 0.30
    if score_event_proxy is not None and (fv_lift is None or fv_lift > 0.01):
        phantom_risk_score += (1.0 - score_event_proxy) * 0.15
    phantom_risk_score = min(1.0, max(0.0, phantom_risk_score))

    candidate_payload["shadow_fv_current_state"] = current_fv_float
    candidate_payload["shadow_fv_after_inferred_score"] = float(after_event_fv)
    candidate_payload["shadow_fv_inferred_lift"] = fv_lift
    candidate_payload["shadow_no_event_edge"] = no_event_edge
    candidate_payload["shadow_after_event_edge"] = after_event_edge
    candidate_payload["shadow_p_score_event_proxy"] = score_event_proxy
    candidate_payload["shadow_phantom_risk_score"] = phantom_risk_score
    candidate_payload["shadow_phantom_risk_band"] = (
        "high" if phantom_risk_score >= 0.70 else "medium" if phantom_risk_score >= 0.40 else "low"
    )
    candidate_payload["shadow_transition_model"] = "score_event_vs_no_event_proxy_v1"
    candidate_payload["shadow_transition_inferred_runs"] = int(inferred_runs)
