#!/usr/bin/env python3
"""
signal_pipeline_gates_post_fv.py -- FV inference + gates that fire AFTER FV.

Free functions extracted from signal_pipeline.py (Tier 4 refactor, 2026-05-01).
Hosts the two phases that run once the model can be invoked:

  1. run_inference_and_fv_phase
       Run-count inference (try +1/+2/+3 runs, pick best ask-distance), apply
       Stage-2 (park/weather) and Stage-3 (team offense) adjustments, run
       probability calibration, compute edge and min_edge. Stops on Gate 8f
       (FV saturation) and Gate 8h (Stage-2 extreme suppression).
  2. evaluate_post_fv_gates
       gate_min_edge -> gate_extreme_edge (TR17, enforced) ->
       gate_fv_ask_gap -> gate_sp_era -> gate_event_dedup -> gate_inning_dedup.
       Returns True when the tick should stop.

Conceptual cut: "does the model output justify a trade?". Pre-FV phase lives
in signal_pipeline_gates_pre_fv.py.

Engine attrs read:
  - engine.cache (lookup / lookup_with_meta)
  - engine.stage2_model, engine.offense_model
  - engine.trade_args.* (many; see individual gates)
  - engine._last_bet_ts, engine._last_bet_edge, engine._last_bet_inning,
    engine._last_bet_edge_by_line
Engine method calls (preserved as method calls so subclass overrides work):
  - engine._calibrate_fair_value(...)
  - engine._log_skip_debug_once(...)
"""

from __future__ import annotations

import logging
import math
from collections import Counter
from typing import Any, Callable, Dict, List, Optional, TYPE_CHECKING

from analyze_polymarket_overreactions import _inning_state_to_half
from line_state import _ask_edge_boost
from runtime_log_rollups import record_runtime_debug_rollup
from signal_config import (
    DEFAULT_ASK_EDGE_RAMP_ENABLED,
    DEFAULT_ASK_EDGE_RAMP_END,
    DEFAULT_ASK_EDGE_RAMP_MAX_BOOST,
    DEFAULT_ASK_EDGE_RAMP_START,
    DEFAULT_EXTREME_EDGE_MAX,
    STAGE1_ALT_A_SCOPE_RULES,
    STAGE1_ALT_A_SCOPE_DEFAULT_ACTION,
)
from stage1_cache_audit import resolve_cell_line_probability
from stage1_support import prefixed_stage1_support_fields
from stage2_run_env_model import RunEnvContext
from model_families import SCORE_EVENT_TRANSITION
from weather_client import (
    weather_v2_features_model_usable,
    weather_v2_run_env_game_data,
    weather_v2_source_label,
)

from signal_pipeline_payload import attach_shadow_risk_tags as _attach_shadow_risk_tags
from signal_pipeline_state_value import (
    attach_score_transition_shadow_fields as _attach_score_transition_shadow_fields,
    compute_state_value_snapshot as _compute_state_value_snapshot,
)

if TYPE_CHECKING:
    from signal_engine import SignalEngine
    from signal_pipeline import FvPhaseResult, TickContext

LOGGER = logging.getLogger("signal_engine")


def _cell_from_lookup_meta(
    self: "SignalEngine",
    *,
    lookup_meta: Optional[Dict[str, object]],
) -> tuple[Optional[Dict[str, Any]], Dict[str, object]]:
    meta = lookup_meta if isinstance(lookup_meta, dict) else {}
    cell_key = meta.get("state_cell_key")
    cell = None
    cells = getattr(getattr(self, "cache", None), "cells", None)
    if cell_key is not None and isinstance(cells, dict):
        raw_cell = cells.get(str(cell_key))
        if isinstance(raw_cell, dict):
            cell = raw_cell
    return cell, meta


def _num_from_cell(cell: Optional[Dict[str, Any]], name: str) -> Optional[float]:
    if cell is None:
        return None
    try:
        raw = cell.get(name)
        return float(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None


def _empirical_from_lookup_meta(
    self: "SignalEngine",
    *,
    line: str,
    lookup_meta: Optional[Dict[str, object]],
) -> tuple[Optional[float], Dict[str, object], Optional[Dict[str, Any]], Dict[str, object]]:
    """Resolve diagnostic empirical sibling probability for one lookup result."""
    cell, meta = _cell_from_lookup_meta(self, lookup_meta=lookup_meta)

    empirical_meta: Dict[str, object] = {}
    empirical_prob = None
    if cell is not None:
        empirical_prob, empirical_meta = resolve_cell_line_probability(
            cell,
            requested_line=line,
            prefix="o",
        )
    return empirical_prob, empirical_meta, cell, meta


def _attach_inferred_state_stage1_audit(
    self: "SignalEngine",
    *,
    candidate_payload: Dict[str, object],
    line: str,
    ask: float,
    poisson_prob: float,
    lookup_meta: Optional[Dict[str, object]],
) -> None:
    """Log the empirical sibling/support for the inferred Stage-1 cell.

    The runtime Stage-1 lookup intentionally returns the `poXX` probability.
    These fields expose the matching `oXX` empirical rate and support counts so
    post-run audits can measure whether Poisson fallback is overconfident.
    """
    empirical_prob, empirical_meta, cell, meta = _empirical_from_lookup_meta(
        self,
        line=line,
        lookup_meta=lookup_meta,
    )

    candidate_payload["inferred_state_base_poisson"] = float(poisson_prob)
    candidate_payload["inferred_state_base_empirical"] = empirical_prob
    candidate_payload["inferred_state_poisson_minus_empirical"] = (
        float(poisson_prob) - empirical_prob if empirical_prob is not None else None
    )
    candidate_payload["inferred_state_empirical_edge"] = (
        empirical_prob - ask if empirical_prob is not None else None
    )
    candidate_payload["inferred_state_n"] = _num_from_cell(cell, "n")
    candidate_payload["inferred_state_n_samples"] = _num_from_cell(cell, "n_samples")
    candidate_payload["inferred_state_weighted_n"] = _num_from_cell(cell, "weighted_n")
    candidate_payload["inferred_state_effective_n"] = _num_from_cell(cell, "effective_n")
    candidate_payload["inferred_state_fallback_level"] = meta.get("state_fallback_level")
    candidate_payload["inferred_state_fallback_label"] = meta.get("state_fallback_label")
    candidate_payload["inferred_state_cell_key"] = meta.get("state_cell_key")
    candidate_payload["inferred_state_line_key_poisson"] = meta.get("line_source_key")
    candidate_payload["inferred_state_line_key_empirical"] = empirical_meta.get("line_requested_key")
    candidate_payload["inferred_state_line_fallback_mode"] = meta.get("line_fallback_mode")
    candidate_payload["inferred_state_empirical_line_fallback_mode"] = empirical_meta.get("line_fallback_mode")
    candidate_payload["inferred_state_empirical_line_source_key"] = empirical_meta.get("line_source_key")
    candidate_payload["inferred_state_empirical_line_source_key_low"] = empirical_meta.get("line_source_key_low")
    candidate_payload["inferred_state_empirical_line_source_key_high"] = empirical_meta.get("line_source_key_high")
    candidate_payload["inferred_state_used_fallback"] = bool(meta.get("used_fallback"))
    candidate_payload["inferred_state_base_source"] = "poisson_runtime"
    candidate_payload.update(
        prefixed_stage1_support_fields(
            prefix="inferred_state",
            cell=cell,
            state_fallback_level=meta.get("state_fallback_level"),
            poisson_line_fallback_mode=meta.get("line_fallback_mode"),
            empirical_line_fallback_mode=empirical_meta.get("line_fallback_mode"),
        )
    )


def _attach_run_count_inference_panel(
    *,
    candidate_payload: Dict[str, object],
    options: List[Dict[str, object]],
    selected_runs: Optional[int],
) -> None:
    """Attach compact +1/+2/+3 inference alternatives for FV gap audits."""
    candidate_payload["inference_panel_runs_considered"] = ",".join(
        str(opt.get("runs")) for opt in options if opt.get("runs") is not None
    )
    candidate_payload["inference_panel_selected_rule"] = "min_abs_poisson_minus_ask"
    candidate_payload["inference_panel_selected_runs"] = selected_runs
    for opt in options:
        runs = opt.get("runs")
        if runs not in (1, 2, 3):
            continue
        prefix = f"inference_run{runs}"
        candidate_payload[f"{prefix}_selected"] = bool(selected_runs == runs)
        for key in (
            "away_score",
            "home_score",
            "total",
            "base_poisson",
            "base_empirical",
            "poisson_minus_empirical",
            "distance_to_ask",
            "empirical_distance_to_ask",
            "n",
            "n_samples",
            "effective_n",
            "support_effective_n_proxy",
            "support_stage1_trust_weight",
            "support_stage1_support_bucket",
            "support_exact_cell_support",
            "support_poisson_line_exact",
            "support_empirical_line_exact",
            "support_empirical_sample_support",
            "support_empirical_sample_bucket",
            "support_state_fallback_penalty",
            "support_line_fallback_penalty",
            "fallback_level",
            "fallback_label",
            "cell_key",
            "line_fallback_mode",
            "line_source_key",
            "empirical_line_fallback_mode",
            "empirical_line_source_key",
        ):
            candidate_payload[f"{prefix}_{key}"] = opt.get(key)


def _rollup_state_label(
    *,
    half: str,
    inning: int,
    outs: int,
    away_score: int,
    home_score: int,
    runners_on: int,
) -> str:
    return (
        f"{half}{inning}|outs={outs}|score={away_score}-{home_score}|"
        f"runners={runners_on}"
    )


# ---------------------------------------------------------------------------
# Active #8 prep (2026-05-17): Stage-1 shadow empirical override helper.
# ---------------------------------------------------------------------------


def _stage1_shadow_logit(p: float) -> float:
    _eps = 1e-6
    p = max(_eps, min(1.0 - _eps, p))
    return math.log(p / (1.0 - p))


def _stage1_shadow_sigmoid(x: float) -> float:
    if x >= 0:
        return 1.0 / (1.0 + math.exp(-x))
    z = math.exp(x)
    return z / (1.0 + z)


def _attach_stage1_shadow_empirical_fields(
    self: "SignalEngine",
    *,
    candidate_payload: Dict[str, object],
    base_fair_value_poisson: float,
    stage2_run_env_delta: float,
    team_offense_delta: float,
    line: str,
    inning: int,
    decision_ask: float,
) -> None:
    """Attach Stage-1 shadow empirical override diagnostics.

    When the engine is in `shadow` mode AND the inferred cell carries
    an empirical estimate, computes the alt FV under "prefer empirical
    over Poisson" and attaches both raw and calibrated alt FVs to the
    candidate payload alongside production fields. Per-tick logging;
    NO decision change.

    When the mode is `off` (default) OR no empirical is available,
    attaches only the mode tag so downstream consumers can distinguish
    "shadow ran but no empirical" from "shadow off." Fail-open: ANY
    exception is logged at DEBUG and silently swallowed; production
    fair_value is unaffected.

    Schema additions:
      - `stage1_shadow_empirical_mode` : 'off' / 'shadow'
      - `fair_value_alt_empirical` : alt p3 (post calibration) when
        mode=shadow AND empirical available; else None
      - `fair_value_alt_empirical_raw` : alt p2 (pre calibration);
        the value before the calibrator runs
      - `fair_value_alt_empirical_delta_vs_prod` : alt_p3 - prod_p3
        (signed; negative = alt is more conservative than production)
      - `fair_value_alt_empirical_used_empirical` : True/False
      - `fair_value_alt_empirical_p0` : the empirical input used
    """
    mode = getattr(self, "_stage1_shadow_empirical_mode", "off")
    candidate_payload["stage1_shadow_empirical_mode"] = mode
    candidate_payload["fair_value_alt_empirical"] = None
    candidate_payload["fair_value_alt_empirical_raw"] = None
    candidate_payload["fair_value_alt_empirical_delta_vs_prod"] = None
    candidate_payload["fair_value_alt_empirical_used_empirical"] = False
    candidate_payload["fair_value_alt_empirical_p0"] = None

    if mode != "shadow":
        return

    empirical_raw = candidate_payload.get("inferred_state_base_empirical")
    try:
        empirical = float(empirical_raw) if empirical_raw is not None else None
    except (TypeError, ValueError):
        empirical = None
    if empirical is None:
        return
    if not (0.0 < empirical < 1.0):
        # Boundary values would blow up the logit transform; leave the
        # alt fields None and the operator can see "shadow ran but
        # empirical was unusable."
        return

    try:
        # Step 1: replace Stage-1 Poisson with empirical, apply
        # Stage-2 + Stage-3 logit-additive deltas. This matches the
        # offline shadow-override report's math exactly.
        p2_alt = _stage1_shadow_sigmoid(
            _stage1_shadow_logit(empirical)
            + stage2_run_env_delta
            + team_offense_delta
        )

        # Step 2: calibrate p2_alt through the SAME calibrator
        # production uses. Keeps the alt FV directly comparable to
        # the production fair_value (which is post-calibration).
        # Currently calibration is in shadow mode and applies no
        # shift; the calibrator call is cheap and future-proofs the
        # comparison when calibration eventually flips to enforce.
        p3_alt = p2_alt
        calibrated_fv, _ = self._calibrate_fair_value(
            raw_prob=p2_alt,
            line=line,
            inning=inning,
            decision_ask=decision_ask,
            model_family=SCORE_EVENT_TRANSITION,
        )
        if calibrated_fv is not None:
            p3_alt = float(calibrated_fv)

        prod_p3 = candidate_payload.get("fair_value")
        delta = (
            p3_alt - float(prod_p3) if prod_p3 is not None else None
        )

        candidate_payload["fair_value_alt_empirical"] = p3_alt
        candidate_payload["fair_value_alt_empirical_raw"] = p2_alt
        candidate_payload["fair_value_alt_empirical_delta_vs_prod"] = (
            delta
        )
        candidate_payload["fair_value_alt_empirical_used_empirical"] = True
        candidate_payload["fair_value_alt_empirical_p0"] = empirical
    except Exception as exc:  # noqa: BLE001
        LOGGER.debug(
            "Stage-1 shadow empirical override failed for "
            "cell empirical=%s s2=%s s3=%s: %r",
            empirical, stage2_run_env_delta, team_offense_delta, exc,
        )
        # Already initialized to None above; leave them as-is.


def flush_scoped_alt_a_rollup(
    engine: Any,
    *,
    force: bool = False,
    interval_secs: float = 1800.0,
) -> Dict[str, int]:
    """Emit a single INFO-level rollup summary for the Scoped Alt-A
    apply-event counts that have been accumulating since the last flush
    (or since startup). Returns a {dedup_key_str: count} summary the
    caller can include in session JSON; clears engine state on exit.

    Called periodically from `_on_tick_batch` (30-min cadence, matches
    the runtime_log_rollups pattern) and force=True on shutdown so the
    operator can audit the suppressed fire count without scrolling
    through 2,000+ duplicate INFO lines.

    No-op when no rollup state exists or counts are empty.
    """
    import time

    now = time.time()
    last_ts = float(getattr(engine, "_last_scoped_alt_a_rollup_log_ts", 0.0) or 0.0)
    if not force and (now - last_ts) < interval_secs:
        return {}

    counts: Counter = getattr(engine, "_scoped_alt_a_rollup_counts", Counter())
    if not counts:
        engine._last_scoped_alt_a_rollup_log_ts = now
        return {}
    total = sum(counts.values())
    unique_keys = len(counts)
    LOGGER.info(
        "scoped_alt_a rollup: %d suppressed apply ticks across %d unique "
        "(game,line,inning,state,rule) cohorts (after first INFO per cohort).",
        total, unique_keys,
    )
    # Detail at DEBUG: per-cohort count, top 25 by frequency.
    summary: Dict[str, int] = {}
    for key, n in counts.most_common(25):
        label = "|".join(key)
        summary[label] = int(n)
        LOGGER.debug("scoped_alt_a rollup detail: %s -> %d ticks", label, n)
    # Clear state so the next window starts fresh.
    engine._scoped_alt_a_rollup_counts = Counter()
    engine._last_scoped_alt_a_rollup_log_ts = now
    return summary


def _apply_stage1_alt_a_scope(
    self: "SignalEngine",
    *,
    candidate_payload: Dict[str, object],
    current_best_fv: float,
    inning: int,
) -> float:
    """Active #17 (2026-05-21): Scoped Alt-A enforce.

    Reads `_stage1_alt_a_scope_mode` from the engine. When `enforce`,
    consults the cohort-rule list in `STAGE1_ALT_A_SCOPE_RULES`. If a
    rule says `apply` for this candidate's cohort AND the upstream
    `_attach_stage1_shadow_empirical_fields` successfully computed an
    alt empirical FV, swap the production fair_value to use it.

    Always logs the scope decision so operators can audit in shadow
    mode before flipping to enforce.

    Returns the (possibly swapped) best_fv. Mutates
    `candidate_payload` in place when a swap happens:
      - `fair_value` <- alt empirical FV
      - `inferred_state_base_source` <- 'empirical_scoped_alt_a'
      - `stage1_alt_a_scope_decision` <- 'applied'

    Fail-open: any exception is logged at debug and the original
    Poisson FV is preserved.
    """
    mode = str(getattr(self, "_stage1_alt_a_scope_mode", "off"))
    candidate_payload["stage1_alt_a_scope_mode"] = mode
    candidate_payload["stage1_alt_a_scope_decision"] = "mode_off"
    candidate_payload["stage1_alt_a_scope_rule_matched"] = None

    if mode == "off":
        return current_best_fv

    # Resolve the action from the rule list.
    # 2026-05-24 (audit followup): cohort_values now carries every
    # available dimension as a dict; predicates pull what they need.
    # Was single-value before (per-rule `dimension` field + scalar
    # predicate); the new shape supports multi-dim rules like
    # `inning in (6,7) AND fallback_level >= 2`.
    cohort_values: Dict[str, Any] = {
        "inning": inning,
        "fallback_level": candidate_payload.get("inferred_state_fallback_level"),
    }
    action = STAGE1_ALT_A_SCOPE_DEFAULT_ACTION
    matched_rule_name = None
    matched_rule_reason = None
    try:
        for rule in STAGE1_ALT_A_SCOPE_RULES:
            pred = rule.get("predicate")
            if not callable(pred):
                continue
            if pred(cohort_values):
                action = str(rule.get("action") or action)
                matched_rule_name = str(rule.get("name") or "")
                matched_rule_reason = str(rule.get("reason") or "")
                break
    except Exception as exc:  # noqa: BLE001
        LOGGER.debug("scope rule eval failed: %r", exc)
        candidate_payload["stage1_alt_a_scope_decision"] = "rule_eval_error"
        return current_best_fv

    candidate_payload["stage1_alt_a_scope_action"] = action
    candidate_payload["stage1_alt_a_scope_rule_matched"] = matched_rule_name

    # If shadow mode, log the decision but never swap.
    if mode == "shadow":
        if action == "apply":
            candidate_payload["stage1_alt_a_scope_decision"] = "would_apply_in_shadow"
        else:
            candidate_payload["stage1_alt_a_scope_decision"] = "held_poisson_in_shadow"
        return current_best_fv

    # mode == enforce. Need the alt empirical FV to have been computed.
    if action != "apply":
        candidate_payload["stage1_alt_a_scope_decision"] = "held_poisson_enforce"
        return current_best_fv
    alt_fv = candidate_payload.get("fair_value_alt_empirical")
    used_empirical = bool(
        candidate_payload.get("fair_value_alt_empirical_used_empirical")
    )
    if alt_fv is None or not used_empirical:
        # Shadow path didn't run or empirical was boundary/missing.
        # Keep Poisson FV.
        candidate_payload["stage1_alt_a_scope_decision"] = "alt_fv_unavailable"
        return current_best_fv

    try:
        new_fv = float(alt_fv)
    except (TypeError, ValueError):
        candidate_payload["stage1_alt_a_scope_decision"] = "alt_fv_unparseable"
        return current_best_fv

    # Swap.
    candidate_payload["fair_value"] = new_fv
    candidate_payload["inferred_state_base_source"] = "empirical_scoped_alt_a"
    candidate_payload["stage1_alt_a_scope_decision"] = "applied"
    if matched_rule_reason:
        candidate_payload["stage1_alt_a_scope_reason"] = matched_rule_reason

    # 2026-05-26 (F2 fix): emit INFO once per (game, line, inning, state, rule)
    # then roll up subsequent ticks. Before this, the same swap fired every
    # ~3s for every active line, accounting for ~85% of the launch log
    # (A_current 2026-05-25: 2,270 of 2,678 lines were repeated Scoped Alt-A
    # INFOs). The dedup key is the cohort that meaningfully changes the
    # decision; same state -> same swap, no operator-actionable signal in
    # repeating it.
    rule_label = matched_rule_name or "default"
    dedup_key = (
        str(candidate_payload.get("game_pk", "?")),
        str(candidate_payload.get("line", "?")),
        str(inning),
        str(candidate_payload.get("inning_state", "?")),
        rule_label,
    )
    if not hasattr(self, "_scoped_alt_a_applied_keys"):
        self._scoped_alt_a_applied_keys = set()
        self._scoped_alt_a_rollup_counts: Counter = Counter()

    if dedup_key not in self._scoped_alt_a_applied_keys:
        self._scoped_alt_a_applied_keys.add(dedup_key)
        LOGGER.info(
            "Scoped Alt-A applied: poisson_fv=%.4f -> empirical_fv=%.4f "
            "(inning=%s, matched_rule=%s)",
            current_best_fv, new_fv, inning, rule_label,
        )
    else:
        self._scoped_alt_a_rollup_counts[dedup_key] += 1
    return new_fv


def run_inference_and_fv_phase(
    self: "SignalEngine",
    ctx: "TickContext",
    candidate_payload: Dict[str, object],
    record_skip: Callable[..., None],
) -> "FvPhaseResult":
    """Infer scored runs, apply FV model stack, and stop on FV-stage gates."""
    # FvPhaseResult lives in signal_pipeline.py; imported lazily to avoid a
    # runtime circular import.
    from signal_pipeline import FvPhaseResult

    game = ctx.game
    market = ctx.market
    inning = ctx.inning
    inning_state = ctx.inning_state
    outs = ctx.outs
    away_score = ctx.away_score
    home_score = ctx.home_score
    current_total = ctx.current_total
    runners_on = ctx.runners_on
    ask = ctx.ask
    line_val = ctx.line_val

    # --- Inference: which team is batting, how many runs scored ---
    half = _inning_state_to_half(inning_state)
    batting_is_away = (half == "T")

    best_run_count, best_fv, best_fv_dist = None, None, float("inf")
    best_lookup_meta: Optional[Dict[str, object]] = None
    last_lookup_meta: Optional[Dict[str, object]] = None
    inference_options: List[Dict[str, object]] = []
    for runs in (1, 2, 3):
        new_away = away_score + runs if batting_is_away else away_score
        new_home = home_score if batting_is_away else home_score + runs
        lookup_with_meta = getattr(self.cache, "lookup_with_meta", None)
        if callable(lookup_with_meta):
            fv, lookup_meta = lookup_with_meta(
                away_score=new_away,
                home_score=new_home,
                inning=inning,
                inning_state=inning_state,
                outs=outs,
                line=market.line,
                runners_on=runners_on,
            )
        else:
            fv = self.cache.lookup(
                away_score=new_away,
                home_score=new_home,
                inning=inning,
                inning_state=inning_state,
                outs=outs,
                line=market.line,
                runners_on=runners_on,
            )
            lookup_meta = None
        last_lookup_meta = lookup_meta
        empirical_prob, empirical_meta, cell, meta = _empirical_from_lookup_meta(
            self,
            line=market.line,
            lookup_meta=lookup_meta,
        )
        option: Dict[str, object] = {
            "runs": runs,
            "away_score": new_away,
            "home_score": new_home,
            "total": new_away + new_home,
            "base_poisson": fv,
            "base_empirical": empirical_prob,
            "poisson_minus_empirical": (
                float(fv) - empirical_prob
                if fv is not None and empirical_prob is not None
                else None
            ),
            "distance_to_ask": abs(float(fv) - ask) if fv is not None else None,
            "empirical_distance_to_ask": (
                abs(empirical_prob - ask) if empirical_prob is not None else None
            ),
            "n": _num_from_cell(cell, "n"),
            "n_samples": _num_from_cell(cell, "n_samples"),
            "effective_n": _num_from_cell(cell, "effective_n"),
            "fallback_level": meta.get("state_fallback_level"),
            "fallback_label": meta.get("state_fallback_label"),
            "cell_key": meta.get("state_cell_key"),
            "line_fallback_mode": meta.get("line_fallback_mode"),
            "line_source_key": meta.get("line_source_key"),
            "empirical_line_fallback_mode": empirical_meta.get("line_fallback_mode"),
            "empirical_line_source_key": empirical_meta.get("line_source_key"),
        }
        option.update(
            prefixed_stage1_support_fields(
                prefix="support",
                cell=cell,
                state_fallback_level=meta.get("state_fallback_level"),
                poisson_line_fallback_mode=meta.get("line_fallback_mode"),
                empirical_line_fallback_mode=empirical_meta.get("line_fallback_mode"),
            )
        )
        inference_options.append(option)
        if fv is None:
            continue
        dist = abs(float(fv) - ask)
        if dist < best_fv_dist:
            best_fv_dist = dist
            best_fv = fv
            best_run_count = runs
            best_lookup_meta = lookup_meta

    _attach_run_count_inference_panel(
        candidate_payload=candidate_payload,
        options=inference_options,
        selected_runs=best_run_count,
    )

    if best_fv is None or best_run_count is None:
        LOGGER.debug(
            "inference_no_match %s@%s line=%s inn=%s%d outs=%d score=%d-%d "
            "(total=%d) runners=%d ask=%.3f - cache returned None for all 3 inferred run counts",
            game.away_abbrev, game.home_abbrev, market.line,
            half, inning, outs,
            away_score, home_score, current_total,
            runners_on, ask,
        )
        if isinstance(last_lookup_meta, dict):
            candidate_payload["inference_line_requested_key"] = last_lookup_meta.get("line_requested_key")
            candidate_payload["inference_line_fallback_mode"] = last_lookup_meta.get("line_fallback_mode")
            candidate_payload["inference_state_fallback_level"] = last_lookup_meta.get("state_fallback_level")
            candidate_payload["inference_state_fallback_label"] = last_lookup_meta.get("state_fallback_label")
            candidate_payload["inference_state_cell_key"] = last_lookup_meta.get("state_cell_key")
        record_skip("inference_no_match")
        return FvPhaseResult(stopped=True)

    base_fv = best_fv
    candidate_payload["inferred_runs"] = best_run_count
    candidate_payload["base_fair_value"] = base_fv
    _attach_inferred_state_stage1_audit(
        self,
        candidate_payload=candidate_payload,
        line=market.line,
        ask=ask,
        poisson_prob=base_fv,
        lookup_meta=best_lookup_meta,
    )
    if isinstance(best_lookup_meta, dict):
        candidate_payload["inference_used_fallback"] = bool(best_lookup_meta.get("used_fallback"))
        candidate_payload["inference_state_fallback_level"] = best_lookup_meta.get("state_fallback_level")
        candidate_payload["inference_state_fallback_label"] = best_lookup_meta.get("state_fallback_label")
        candidate_payload["inference_state_cell_key"] = best_lookup_meta.get("state_cell_key")
        candidate_payload["inference_line_requested_key"] = best_lookup_meta.get("line_requested_key")
        candidate_payload["inference_line_fallback_mode"] = best_lookup_meta.get("line_fallback_mode")
        candidate_payload["inference_line_source_key"] = best_lookup_meta.get("line_source_key")
        candidate_payload["inference_line_source_key_low"] = best_lookup_meta.get("line_source_key_low")
        candidate_payload["inference_line_source_key_high"] = best_lookup_meta.get("line_source_key_high")
        _fallback_level = best_lookup_meta.get("state_fallback_level") or 0
        if _fallback_level > 0:
            record_runtime_debug_rollup(
                self,
                kind="inference_fallback",
                game_pk=game.game_pk,
                game_label=f"{game.away_abbrev}@{game.home_abbrev}",
                line=market.line,
                inning=inning,
                state=_rollup_state_label(
                    half=half,
                    inning=inning,
                    outs=outs,
                    away_score=away_score,
                    home_score=home_score,
                    runners_on=runners_on,
                ),
                source_key=best_lookup_meta.get("line_source_key"),
                message=(
                    "inference fallback %s@%s line=%s: cell=%s level=%d label=%s "
                    "(line_mode=%s src=%s)"
                ),
                args=(
                    game.away_abbrev, game.home_abbrev, market.line,
                    best_lookup_meta.get("state_cell_key"),
                    _fallback_level,
                    best_lookup_meta.get("state_fallback_label"),
                    best_lookup_meta.get("line_fallback_mode"),
                    best_lookup_meta.get("line_source_key"),
                ),
            )

    # --- GATE 8f: FV saturation skip (TR12) ---
    max_base_fv = self.trade_args.max_base_fv
    if base_fv >= max_base_fv:
        _ask_context = (
            "market CONFIRMS (ask>=0.85, likely real run)" if ask >= 0.85
            else "market CONTRADICTS (ask<0.70, phantom pattern)" if ask < 0.70
            else "market AMBIGUOUS"
        )
        self._log_skip_debug_once(
            reason="gate_fv_saturation",
            game=game,
            market=market,
            inning=inning,
            inning_state=inning_state,
            outs=outs,
            current_total=current_total,
            message="Skip %s@%s line=%s: Gate 8f saturation "
                    "(base_fv=%.3f >= threshold=%.3f) ask=%.3f | %s",
            args=(game.away_abbrev, game.home_abbrev, market.line,
                  base_fv, max_base_fv, ask, _ask_context),
        )
        record_skip(
            "gate_fv_saturation",
            shadow_values={
                "base_fair_value": base_fv,
                "decision_ask": ask,
            },
            secondary_shadow_reason="gate_fv_saturation_v2",
            secondary_shadow_values={"base_fair_value": base_fv, "decision_ask": ask},
        )
        return FvPhaseResult(stopped=True)

    stage2_run_env_delta = 0.0
    if self.stage2_model is not None:
        weather_features = {}
        weather_getter = getattr(self, "_weather_fields_for_game", None)
        if callable(weather_getter):
            weather_features = weather_getter(game.game_pk)
        stage2_weather_source = weather_v2_source_label(weather_features)
        candidate_payload["stage2_weather_source"] = stage2_weather_source
        candidate_payload["stage2_weather_model_usable"] = weather_v2_features_model_usable(
            weather_features or {}
        )
        env_context = RunEnvContext.from_game_data(
            weather_v2_run_env_game_data(
                weather_features,
                venue_name=game.venue_name,
                game_date_utc=game.game_date,
            )
        )
        stage2_line = market.line
        s2_adjusted = self.stage2_model.adjust_line(
            line=stage2_line, base_prob=best_fv, context=env_context
        )
        _eps = 1e-6
        _p0 = max(_eps, min(1.0 - _eps, best_fv))
        _p1 = max(_eps, min(1.0 - _eps, s2_adjusted))
        stage2_run_env_delta = math.log(_p1 / (1.0 - _p1)) - math.log(_p0 / (1.0 - _p0))
        if abs(stage2_run_env_delta) >= 0.01:
            record_runtime_debug_rollup(
                self,
                kind="stage2_adj",
                game_pk=game.game_pk,
                game_label=f"{game.away_abbrev}@{game.home_abbrev}",
                line=market.line,
                inning=inning,
                state=_rollup_state_label(
                    half=half,
                    inning=inning,
                    outs=outs,
                    away_score=away_score,
                    home_score=home_score,
                    runners_on=runners_on,
                ),
                source_key=game.venue_name,
                message=(
                    "Stage-2 adj %s@%s line=%s inn=%d: fv=%.3f -> %.3f  "
                    "(delta=%+.3f  weather_source='%s'  park='%s'  temp_bin='%s'  wind_bin='%s')"
                ),
                args=(
                    game.away_abbrev, game.home_abbrev, market.line, inning,
                    best_fv, s2_adjusted, stage2_run_env_delta,
                    stage2_weather_source, env_context.park, env_context.temp_bin, env_context.wind_bin,
                ),
            )
        best_fv = s2_adjusted

    # --- GATE 8h: Stage-2 extreme suppression skip (TR13) ---
    s2_suppress_max = self.trade_args.s2_suppress_max
    s2_suppress_min_inning = self.trade_args.s2_suppress_min_inning
    if stage2_run_env_delta <= s2_suppress_max and inning >= s2_suppress_min_inning:
        self._log_skip_debug_once(
            reason="gate_stage2_suppression",
            game=game,
            market=market,
            inning=inning,
            inning_state=inning_state,
            outs=outs,
            current_total=current_total,
            message="Skip %s@%s line=%s: Stage-2 extreme suppression gate "
                    "(s2_delta=%.3f <= threshold=%.3f  inn=%d >= %d) - extreme park/weather suppression",
            args=(
                game.away_abbrev, game.home_abbrev, market.line,
                stage2_run_env_delta, s2_suppress_max, inning, s2_suppress_min_inning,
            ),
        )
        candidate_payload["stage2_run_env_delta"] = stage2_run_env_delta
        record_skip(
            "gate_stage2_suppression",
            shadow_values={"stage2_run_env_delta": stage2_run_env_delta, "inning": inning},
        )
        return FvPhaseResult(stopped=True)

    # --- [Stage-3] Team offense adjustment ---
    game_date = game.game_date[:10]
    adjusted_fv = self.offense_model.adjust_fv(
        base_fv=best_fv,
        away_abbrev=game.away_abbrev,
        home_abbrev=game.home_abbrev,
        game_date=game_date,
        inning=inning,
    )
    team_offense_delta = self.offense_model.get_matchup_delta(
        game.away_abbrev, game.home_abbrev, game_date, inning
    )
    if abs(team_offense_delta) >= 0.01:
        record_runtime_debug_rollup(
            self,
            kind="stage3_adj",
            game_pk=game.game_pk,
            game_label=f"{game.away_abbrev}@{game.home_abbrev}",
            line=market.line,
            inning=inning,
            state=_rollup_state_label(
                half=half,
                inning=inning,
                outs=outs,
                away_score=away_score,
                home_score=home_score,
                runners_on=runners_on,
            ),
            source_key=f"{game.away_abbrev}-{game.home_abbrev}|{game_date}",
            message=(
                "Stage-3 adj %s@%s line=%s inn=%d: s1+s2_fv=%.3f -> adj_fv=%.3f  "
                "(delta=%+.3f  away_rpg=%.2f  home_rpg=%.2f  expected=%.2f vs avg=%.2f)"
            ),
            args=(
                game.away_abbrev, game.home_abbrev, market.line, inning,
                best_fv, adjusted_fv, team_offense_delta,
                self.offense_model.get_rpg(game.away_abbrev, game_date),
                self.offense_model.get_rpg(game.home_abbrev, game_date),
                self.offense_model.get_expected_total(game.away_abbrev, game.home_abbrev, game_date),
                self.offense_model.mlb_avg_total,
            ),
        )
    stage3_fv = adjusted_fv
    calibrated_fv, calibration_diag = self._calibrate_fair_value(
        raw_prob=stage3_fv,
        line=market.line,
        inning=inning,
        decision_ask=ask,
        model_family=SCORE_EVENT_TRANSITION,
    )
    best_fv = calibrated_fv
    candidate_payload["fair_value_raw"] = stage3_fv
    candidate_payload["fair_value_calibrated"] = calibration_diag.get("calibrated_prob")
    candidate_payload["fair_value_calibration_delta"] = calibration_diag.get("delta")
    candidate_payload["fair_value_calibration_method"] = calibration_diag.get("method")
    candidate_payload["fair_value_calibration_mode"] = calibration_diag.get("mode")
    candidate_payload["fair_value_calibration_family"] = calibration_diag.get("model_family")
    candidate_payload["fair_value_calibration_applied"] = calibration_diag.get("applied")
    candidate_payload["fair_value"] = best_fv
    candidate_payload["stage2_run_env_delta"] = stage2_run_env_delta
    candidate_payload["team_offense_delta"] = team_offense_delta
    candidate_payload["signal_model_family"] = SCORE_EVENT_TRANSITION
    candidate_payload["state_value_strategy"] = SCORE_EVENT_TRANSITION

    # Active #8 prep (2026-05-17): Stage-1 shadow empirical override.
    # When the engine is running in `shadow` mode, compute the
    # alternative FV chain replacing the Stage-1 Poisson estimate with
    # the cell's own empirical rate (when available), then run the same
    # Stage-2 / Stage-3 / calibration pipeline on top. Log both
    # production and alt FV on the candidate row. NO effect on
    # decisions -- pure logging. Fail-open contract: any exception in
    # the alt computation is swallowed; production FV stays the source
    # of truth.
    _attach_stage1_shadow_empirical_fields(
        self,
        candidate_payload=candidate_payload,
        base_fair_value_poisson=base_fv,
        stage2_run_env_delta=stage2_run_env_delta,
        team_offense_delta=team_offense_delta,
        line=market.line,
        inning=inning,
        decision_ask=ask,
    )

    # Active #17 (2026-05-21): Scoped Alt-A enforce. When mode is
    # `enforce` AND the cohort rules say apply AND the alt empirical
    # FV was successfully computed, swap `best_fv` to use the
    # empirical-based chain. The swap mutates candidate_payload to
    # keep downstream code (gates, logging) consistent.
    best_fv = _apply_stage1_alt_a_scope(
        self,
        candidate_payload=candidate_payload,
        current_best_fv=best_fv,
        inning=inning,
    )

    current_state_snapshot = _compute_state_value_snapshot(self, ctx)
    _attach_score_transition_shadow_fields(
        trade_args=self.trade_args,
        ctx=ctx,
        state_snapshot=current_state_snapshot,
        candidate_payload=candidate_payload,
        after_event_fv=best_fv,
        inferred_runs=int(best_run_count),
    )

    edge = best_fv - ask
    candidate_payload["edge"] = edge
    min_edge_base = (
        self.trade_args.edge_threshold_high_line
        if line_val >= self.trade_args.high_line_cutoff
        else self.trade_args.edge_threshold
    )
    ask_edge_boost = 0.0
    if bool(getattr(self.trade_args, "ask_edge_ramp_enabled", DEFAULT_ASK_EDGE_RAMP_ENABLED)):
        ask_edge_boost = _ask_edge_boost(
            ask=ask,
            start=float(getattr(self.trade_args, "ask_edge_ramp_start", DEFAULT_ASK_EDGE_RAMP_START)),
            end=float(getattr(self.trade_args, "ask_edge_ramp_end", DEFAULT_ASK_EDGE_RAMP_END)),
            max_boost=float(getattr(self.trade_args, "ask_edge_ramp_max_boost", DEFAULT_ASK_EDGE_RAMP_MAX_BOOST)),
        )
    min_edge = min_edge_base + ask_edge_boost
    candidate_payload["min_edge_base"] = min_edge_base
    candidate_payload["min_edge_ask_boost"] = ask_edge_boost
    candidate_payload["min_edge_effective"] = min_edge

    return FvPhaseResult(
        stopped=False,
        half=half,
        batting_is_away=batting_is_away,
        inferred_runs=int(best_run_count),
        base_fair_value=base_fv,
        fair_value=best_fv,
        fair_value_raw=stage3_fv,
        stage2_run_env_delta=stage2_run_env_delta,
        team_offense_delta=team_offense_delta,
        edge=edge,
        min_edge_base=min_edge_base,
        ask_edge_boost=ask_edge_boost,
        min_edge=min_edge,
    )


def evaluate_post_fv_gates(
    self: "SignalEngine",
    ctx: "TickContext",
    candidate_payload: Dict[str, object],
    fv_result: "FvPhaseResult",
    record_skip: Callable[..., None],
) -> bool:
    """Evaluate post-FV trade gates. Return True when the tick should stop."""
    game = ctx.game
    market = ctx.market
    inning = ctx.inning
    inning_state = ctx.inning_state
    outs = ctx.outs
    current_total = ctx.current_total
    ask = ctx.ask
    edge = float(fv_result.edge)
    min_edge = float(fv_result.min_edge)

    _attach_shadow_risk_tags(
        trade_args=self.trade_args,
        book=ctx.book,
        candidate_payload=candidate_payload,
        ask=ask,
        edge=edge,
    )

    if edge < min_edge:
        record_skip(
            "gate_min_edge",
            shadow_values={
                "edge": edge,
                "min_edge": min_edge,
                "min_edge_base": fv_result.min_edge_base,
                "ask_edge_boost": fv_result.ask_edge_boost,
                "decision_ask": ask,
            },
        )
        return True

    # --- GATE 8f: Extreme-edge phantom-run protection (TR17, ENFORCED 2026-05-01) ---
    # Promoted from shadow tag to enforced gate based on cumulative live evidence:
    #   edge > 0.25 settled filled bets (through 2026-04-30):
    #       1W / 5L, -$117.56 realized on $140.29 stake.
    #   The losses are concentrated, large per event, and recur across innings --
    #   notably the 2026-04-28 LAA@CWS loss (edge=0.302 in inning 4) which
    #   bypassed gate_fv_ask_gap because that gate only applies inning >= 7.
    # TR19 tightened the runtime default to 0.22 after the 2026-04-28 ->
    # 2026-05-03 window showed edge>0.20 was dominated by phantom-run losses.
    # Asymmetric risk:
    # cost of a false positive (skipping a real high-edge winner) is small in
    # expectation; cost of a false negative is concentrated and material.
    extreme_edge_max = float(getattr(self.trade_args, "extreme_edge_max", DEFAULT_EXTREME_EDGE_MAX))
    if edge > extreme_edge_max:
        self._log_skip_debug_once(
            reason="gate_extreme_edge",
            game=game,
            market=market,
            inning=inning,
            inning_state=inning_state,
            outs=outs,
            current_total=current_total,
            message="Skip %s@%s line=%s: extreme edge in any inning "
                    "(edge=%.3f > threshold=%.3f  inn=%d) - phantom-run risk",
            args=(
                game.away_abbrev, game.home_abbrev, market.line,
                edge, extreme_edge_max, inning,
            ),
        )
        record_skip(
            "gate_extreme_edge",
            shadow_values={
                "edge": edge,
                "extreme_edge_max": extreme_edge_max,
                "inning": inning,
            },
        )
        return True

    # --- GATE 8g: Large FV/ask gap in late innings (TR12) ---
    fv_ask_gap_max = self.trade_args.fv_ask_gap_max
    fv_ask_gap_min_inning = self.trade_args.fv_ask_gap_min_inning
    if edge > fv_ask_gap_max and inning >= fv_ask_gap_min_inning:
        self._log_skip_debug_once(
            reason="gate_fv_ask_gap",
            game=game,
            market=market,
            inning=inning,
            inning_state=inning_state,
            outs=outs,
            current_total=current_total,
            message="Skip %s@%s line=%s: large FV/ask gap in late inning "
                    "(edge=%.3f > threshold=%.3f  inn=%d >= %d) - market disagreement signal",
            args=(
                game.away_abbrev, game.home_abbrev, market.line,
                edge, fv_ask_gap_max, inning, fv_ask_gap_min_inning,
            ),
        )
        record_skip(
            "gate_fv_ask_gap",
            shadow_values={"edge": edge, "inning": inning},
        )
        return True

    # --- GATE 8i: Stage-4 pitcher quality edge boost (TR13) ---
    sp_era_threshold = self.trade_args.sp_era_threshold
    sp_era_max_inning = self.trade_args.sp_era_max_inning
    sp_era_edge_boost = self.trade_args.sp_era_edge_boost
    current_pitcher_era = getattr(game, "current_pitcher_era", 4.20)
    if current_pitcher_era < sp_era_threshold and inning <= sp_era_max_inning:
        boosted_min_edge = min_edge + sp_era_edge_boost
        if edge < boosted_min_edge:
            self._log_skip_debug_once(
                reason="gate_sp_era",
                game=game,
                market=market,
                inning=inning,
                inning_state=inning_state,
                outs=outs,
                current_total=current_total,
                message="Skip %s@%s line=%s: pitcher quality edge boost "
                        "(pitcher_era=%.2f < threshold=%.2f  inn=%d <= %d  "
                        "edge=%.3f < boosted_min=%.3f) - quality pitching raises edge requirement",
                args=(
                    game.away_abbrev, game.home_abbrev, market.line,
                    current_pitcher_era, sp_era_threshold, inning, sp_era_max_inning,
                    edge, boosted_min_edge,
                ),
            )
            record_skip(
                "gate_sp_era",
                shadow_values={"edge": edge, "min_edge": min_edge},
            )
            return True
        LOGGER.debug(
            "Pitcher quality note %s@%s line=%s: pitcher_era=%.2f < %.2f  inn=%d  "
            "edge=%.3f passes boosted_min=%.3f",
            game.away_abbrev, game.home_abbrev, market.line,
            current_pitcher_era, sp_era_threshold, inning,
            edge, boosted_min_edge,
        )

    # --- GATE 9: Same-event dedup - 60s window (TR2 Change 5) ---
    last_ts = self._last_bet_ts.get(game.game_pk, 0.0)
    if (ctx.now - last_ts) < self.trade_args.event_dedup_secs:
        last_edge = self._last_bet_edge.get(game.game_pk, 0.0)
        if edge <= last_edge:
            self._log_skip_debug_once(
                reason="gate_event_dedup",
                game=game,
                market=market,
                inning=inning,
                inning_state=inning_state,
                outs=outs,
                current_total=current_total,
                message="Dedup skip %s@%s line=%s edge=%.3f (same-event, edge=%.3f already bet)",
                args=(game.away_abbrev, game.home_abbrev, market.line, edge, last_edge),
            )
            record_skip("gate_event_dedup")
            return True

    # --- GATE 10: Cross-inning same-line dedup (TR4 Change 2) ---
    line_inning_key = (game.game_pk, market.line)
    last_inning = self._last_bet_inning.get(line_inning_key, -1)
    if last_inning >= 0:
        innings_elapsed = inning - last_inning
        if innings_elapsed < self.trade_args.inning_dedup_gap:
            last_line_edge = self._last_bet_edge_by_line.get(line_inning_key, 0.0)
            edge_improvement = edge - last_line_edge
            if edge_improvement <= self.trade_args.inning_dedup_edge_gap:
                self._log_skip_debug_once(
                    reason="gate_inning_dedup",
                    game=game,
                    market=market,
                    inning=inning,
                    inning_state=inning_state,
                    outs=outs,
                    current_total=current_total,
                    message="Cross-inning dedup skip %s@%s line=%s: "
                            "only %d inning(s) since last bet on this line (need %d), "
                            "edge improvement %.3f <= %.3f",
                    args=(
                        game.away_abbrev, game.home_abbrev, market.line,
                        innings_elapsed, self.trade_args.inning_dedup_gap,
                        edge_improvement, self.trade_args.inning_dedup_edge_gap,
                    ),
                )
                record_skip("gate_inning_dedup")
                return True

    return False
