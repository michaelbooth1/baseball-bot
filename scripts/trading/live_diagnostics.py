#!/usr/bin/env python3
"""
live_diagnostics.py -- Read-only end-of-run diagnostics builders + loggers.

Free functions extracted from live_engine.LiveTradingEngine (Tier 3 refactor,
2026-05-01). All read-only summarization of `engine._bets`; no state mutation
beyond the one-shot `_shadow_order_summary_logged` /
`_current_state_edge_band_summary_logged` flags that prevent duplicate logs.

Engine retains thin method wrappers (`_build_shadow_order_diagnostics`,
`_build_current_state_edge_band_diagnostics`, `_current_state_edge_band`,
`_log_shadow_order_diagnostics`, `_log_current_state_edge_band_diagnostics`)
so test stubs and `_save_session` call-sites continue to work unchanged.

Surfaces:
  - build_shadow_order_diagnostics(engine) -> dict
        Per-regime (high_edge, ltp_ask_gap, fv_saturation, phantom_*,
        current_state_positive) order outcome counts. Diagnostics-only.
  - current_state_edge_band(engine, edge) -> (band_name, label)
  - build_current_state_edge_band_diagnostics(engine) -> dict
        Banded outcomes for score-event orders by current-state edge.
  - build_shadow_feature_diagnostics(engine) -> dict
        Shadow-only bucket cuts for low ask/high edge, rn=3.5,
        current-edge x phantom, inning x runs-needed, and bottom-9 risk.
  - log_shadow_order_diagnostics(engine, diagnostics)
  - log_shadow_feature_diagnostics(engine, diagnostics)
  - log_current_state_edge_band_diagnostics(engine, diagnostics)

Engine attrs read:
  - engine._bets, engine.trade_args, engine.date_str
  - engine._is_bet_executable(bet), engine._filled_notional(bet)
Engine attrs written:
  - engine._shadow_order_summary_logged (one-shot guard)
  - engine._current_state_edge_band_summary_logged (one-shot guard)
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Optional, Tuple, TYPE_CHECKING

from signal_config import DEFAULT_EXTREME_EDGE_MAX, DEFAULT_LTP_ASK_GAP_MAX
from order_status import is_exposure_counted_status as _is_exposure_counted_status
from shadow_diagnostic_features import (
    LOW_ASK_HIGH_EDGE_ASK_MAX,
    LOW_ASK_HIGH_EDGE_EDGE_MIN,
    RUNS_NEEDED_EXACT_TRAP,
    compute_shadow_diagnostic_fields,
    safe_float,
)

if TYPE_CHECKING:
    from live_engine import LiveTradingEngine
    from models import LiveBetRecord

LOGGER = logging.getLogger("live_engine")


def _float_attr(bet: "LiveBetRecord", name: str) -> Optional[float]:
    value = getattr(bet, name, None)
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _str_attr(bet: "LiveBetRecord", name: str) -> str:
    return str(getattr(bet, name, "") or "").lower()


def _runs_needed_for_bet(bet: "LiveBetRecord") -> Optional[float]:
    line = _float_attr(bet, "line")
    away = _float_attr(bet, "away_score_before")
    home = _float_attr(bet, "home_score_before")
    if line is None or away is None or home is None:
        return None
    return line - (away + home)


def _shadow_feature_row_for_bet(bet: "LiveBetRecord") -> Dict[str, Any]:
    row = {
        "entry_ask": getattr(bet, "entry_ask", None),
        "decision_ask": getattr(bet, "decision_ask", None) or getattr(bet, "entry_ask", None),
        "edge": getattr(bet, "edge", None),
        "runs_needed": _runs_needed_for_bet(bet),
        "inning": getattr(bet, "inning", None),
        "current_state_value_edge": getattr(bet, "current_state_value_edge", None),
        "shadow_phantom_risk_score": getattr(bet, "shadow_phantom_risk_score", None),
        "shadow_phantom_risk_band": getattr(bet, "shadow_phantom_risk_band", None),
        "home_leading_late": getattr(bet, "home_leading_late", None),
        "batting_team_is_home": getattr(bet, "batting_team_is_home", None),
        "expected_remaining_pa_bucket": getattr(bet, "expected_remaining_pa_bucket", None),
        "home_skip_bottom9_risk": getattr(bet, "home_skip_bottom9_risk", None),
    }
    row.update(compute_shadow_diagnostic_fields(row))
    return row


def _empty_outcome_row(label: str) -> Dict[str, Any]:
    return {
        "label": label,
        "placed": 0,
        "filled": 0,
        "missed": 0,
        "open": 0,
        "filled_wins": 0,
        "filled_losses": 0,
        "signal_wins": 0,
        "signal_losses": 0,
        "filled_staked": 0.0,
        "filled_profit": 0.0,
        "reserved_on_misses": 0.0,
        "decision_asks": [],
        "edges": [],
        "current_state_value_edges": [],
        "phantom_risk_scores": [],
        "runs_needed_values": [],
        "expected_remaining_half_innings": [],
    }


def _observe_outcome_row(
    engine: "LiveTradingEngine",
    row: Dict[str, Any],
    bet: "LiveBetRecord",
) -> None:
    row["placed"] = int(row["placed"]) + 1
    if engine._is_bet_executable(bet):
        row["filled"] = int(row["filled"]) + 1
        if getattr(bet, "won", None) is True:
            row["filled_wins"] = int(row["filled_wins"]) + 1
        elif getattr(bet, "won", None) is False:
            row["filled_losses"] = int(row["filled_losses"]) + 1
        row["filled_staked"] = float(row["filled_staked"]) + engine._filled_notional(bet)
        row["filled_profit"] = float(row["filled_profit"]) + float(getattr(bet, "profit", 0.0) or 0.0)
    elif (
        not engine._is_bet_executable(bet)
        and getattr(bet, "order_status", "") in {"cancelled", "expired"}
    ):
        row["missed"] = int(row["missed"]) + 1
        row["reserved_on_misses"] = (
            float(row["reserved_on_misses"]) + float(getattr(bet, "stake", 0.0) or 0.0)
        )
    elif _is_exposure_counted_status(getattr(bet, "order_status", "")):
        row["open"] = int(row["open"]) + 1

    if getattr(bet, "won", None) is True:
        row["signal_wins"] = int(row["signal_wins"]) + 1
    elif getattr(bet, "won", None) is False:
        row["signal_losses"] = int(row["signal_losses"]) + 1

    shadow_row = _shadow_feature_row_for_bet(bet)
    for value, list_name in (
        (shadow_row.get("decision_ask"), "decision_asks"),
        (getattr(bet, "edge", None), "edges"),
        (getattr(bet, "current_state_value_edge", None), "current_state_value_edges"),
        (getattr(bet, "shadow_phantom_risk_score", None), "phantom_risk_scores"),
        (shadow_row.get("runs_needed"), "runs_needed_values"),
        (getattr(bet, "expected_remaining_half_innings", None), "expected_remaining_half_innings"),
    ):
        numeric = safe_float(value)
        if numeric is not None:
            row[list_name].append(numeric)


def _finalize_outcome_row(row: Dict[str, Any]) -> Dict[str, Any]:
    placed = int(row["placed"])
    filled = int(row["filled"])
    filled_staked = float(row["filled_staked"])
    row["fill_rate"] = round(filled / placed, 4) if placed else None
    row["filled_roi"] = (
        round(float(row["filled_profit"]) / filled_staked, 4)
        if filled_staked > 0
        else None
    )
    row["filled_staked"] = round(filled_staked, 2)
    row["filled_profit"] = round(float(row["filled_profit"]), 2)
    row["reserved_on_misses"] = round(float(row["reserved_on_misses"]), 2)

    for list_name, output_name in (
        ("decision_asks", "avg_decision_ask"),
        ("edges", "avg_edge"),
        ("current_state_value_edges", "avg_current_state_value_edge"),
        ("phantom_risk_scores", "avg_phantom_risk_score"),
        ("runs_needed_values", "avg_runs_needed"),
        ("expected_remaining_half_innings", "avg_expected_remaining_half_innings"),
    ):
        values = list(row.pop(list_name))
        row[output_name] = round(sum(values) / len(values), 4) if values else None
    return row


# ---------------------------------------------------------------------------
# Shadow risk regime diagnostics
# ---------------------------------------------------------------------------

def build_shadow_order_diagnostics(
    engine: "LiveTradingEngine",
) -> Dict[str, Dict[str, object]]:
    """Summarize non-enforcing risk regimes across placed live orders."""
    extreme_edge_max = float(getattr(engine.trade_args, "extreme_edge_max", DEFAULT_EXTREME_EDGE_MAX))
    ltp_ask_gap_max = float(getattr(engine.trade_args, "ltp_ask_gap_max", DEFAULT_LTP_ASK_GAP_MAX))
    max_base_fv = float(getattr(engine.trade_args, "max_base_fv", 0.99))

    regimes = {
        "high_edge": {
            "label": f"edge>{extreme_edge_max:.3f}",
            "predicate": lambda b: (_float_attr(b, "edge") or 0.0) > extreme_edge_max,
        },
        "ltp_ask_gap": {
            "label": f"abs(ask-ltp)>{ltp_ask_gap_max:.3f}",
            "predicate": lambda b: (
                _float_attr(b, "ltp_at_signal") is not None
                and _float_attr(b, "entry_ask") is not None
                and abs(float(_float_attr(b, "entry_ask")) - float(_float_attr(b, "ltp_at_signal"))) > ltp_ask_gap_max
            ),
        },
        "fv_saturation": {
            "label": f"base_fv>={max_base_fv:.3f}",
            "predicate": lambda b: (_float_attr(b, "base_fair_value") or 0.0) >= max_base_fv,
        },
        "phantom_high": {
            "label": "phantom_risk=high",
            "predicate": lambda b: _str_attr(b, "shadow_phantom_risk_band") == "high",
        },
        "phantom_high_current_negative": {
            "label": "phantom=high & current_edge<0",
            "predicate": lambda b: (
                _str_attr(b, "shadow_phantom_risk_band") == "high"
                and _float_attr(b, "current_state_value_edge") is not None
                and float(_float_attr(b, "current_state_value_edge")) < 0.0
            ),
        },
        "current_state_positive": {
            "label": "current_edge>=0",
            "predicate": lambda b: (
                _float_attr(b, "current_state_value_edge") is not None
                and float(_float_attr(b, "current_state_value_edge")) >= 0.0
            ),
        },
    }

    out: Dict[str, Dict[str, object]] = {}
    for name, config in regimes.items():
        selected = [
            b for b in engine._bets
            if getattr(b, "order_id", None) and config["predicate"](b)
        ]
        filled = [b for b in selected if engine._is_bet_executable(b)]
        missed = [
            b for b in selected
            if not engine._is_bet_executable(b)
            and getattr(b, "order_status", "") in {"cancelled", "expired"}
        ]
        open_orders = [
            b for b in selected
            if _is_exposure_counted_status(getattr(b, "order_status", ""))
        ]
        filled_wins = sum(1 for b in filled if getattr(b, "won", None) is True)
        filled_losses = sum(1 for b in filled if getattr(b, "won", None) is False)
        signal_wins = sum(1 for b in selected if getattr(b, "won", None) is True)
        signal_losses = sum(1 for b in selected if getattr(b, "won", None) is False)
        filled_profit = sum(float(getattr(b, "profit", 0.0) or 0.0) for b in filled)
        filled_staked = sum(engine._filled_notional(b) for b in filled)
        reserved_on_misses = sum(float(getattr(b, "stake", 0.0) or 0.0) for b in missed)
        out[name] = {
            "label": config["label"],
            "placed": len(selected),
            "filled": len(filled),
            "missed": len(missed),
            "open": len(open_orders),
            "fill_rate": round(len(filled) / len(selected), 4) if selected else None,
            "filled_wins": filled_wins,
            "filled_losses": filled_losses,
            "signal_wins": signal_wins,
            "signal_losses": signal_losses,
            "filled_staked": round(filled_staked, 2),
            "filled_profit": round(filled_profit, 2),
            "reserved_on_misses": round(reserved_on_misses, 2),
        }
    return out


# ---------------------------------------------------------------------------
# Shadow feature bucket diagnostics
# ---------------------------------------------------------------------------

def _selected_outcome_summary(
    engine: "LiveTradingEngine",
    label: str,
    predicate: Callable[["LiveBetRecord", Dict[str, Any]], bool],
) -> Dict[str, Any]:
    row = _empty_outcome_row(label)
    for bet in engine._bets:
        if not getattr(bet, "order_id", None):
            continue
        shadow_row = _shadow_feature_row_for_bet(bet)
        if predicate(bet, shadow_row):
            _observe_outcome_row(engine, row, bet)
    return _finalize_outcome_row(row)


def _bucketed_outcome_summary(
    engine: "LiveTradingEngine",
    label_prefix: str,
    bucket_field: str,
) -> Dict[str, Dict[str, Any]]:
    buckets: Dict[str, Dict[str, Any]] = {}
    for bet in engine._bets:
        if not getattr(bet, "order_id", None):
            continue
        shadow_row = _shadow_feature_row_for_bet(bet)
        bucket = str(shadow_row.get(bucket_field) or "missing")
        if bucket == "missing":
            continue
        row = buckets.setdefault(bucket, _empty_outcome_row(f"{label_prefix}: {bucket}"))
        _observe_outcome_row(engine, row, bet)
    return {
        key: _finalize_outcome_row(row)
        for key, row in sorted(buckets.items(), key=lambda item: item[0])
    }


def build_shadow_feature_diagnostics(
    engine: "LiveTradingEngine",
) -> Dict[str, Dict[str, object]]:
    """Summarize new shadow-only risk cuts across placed live orders."""
    return {
        "schema_version": 1,
        "regimes": {
            "low_ask_high_edge": _selected_outcome_summary(
                engine,
                f"ask<{LOW_ASK_HIGH_EDGE_ASK_MAX:.2f} & edge>{LOW_ASK_HIGH_EDGE_EDGE_MIN:.2f}",
                lambda _bet, row: row.get("shadow_low_ask_high_edge") is True,
            ),
            "runs_needed_exact_3p5": _selected_outcome_summary(
                engine,
                f"runs_needed={RUNS_NEEDED_EXACT_TRAP:.1f}",
                lambda _bet, row: row.get("shadow_runs_needed_exact_3p5") is True,
            ),
            "home_skip_bottom9_risk": _selected_outcome_summary(
                engine,
                "home-leading late state may skip bottom 9th",
                lambda _bet, row: row.get("shadow_home_skip_bottom9_risk_bucket") == "skip_bottom9_risk",
            ),
        },
        "current_phantom_combo": _bucketed_outcome_summary(
            engine,
            "current_edge x phantom",
            "shadow_current_phantom_combo_bucket",
        ),
        "inning_runs_needed_combo": _bucketed_outcome_summary(
            engine,
            "inning x runs_needed",
            "shadow_inning_runs_needed_bucket",
        ),
        "bottom9_home_lead_context": _bucketed_outcome_summary(
            engine,
            "bottom9/home-lead context",
            "shadow_bottom9_home_lead_context",
        ),
    }


# ---------------------------------------------------------------------------
# Current-state edge band diagnostics
# ---------------------------------------------------------------------------

def current_state_edge_band(
    engine: "LiveTradingEngine",
    edge: Optional[float],
) -> Tuple[str, str]:
    """Bucket score-event current-state edge for shadow end-of-run audits."""
    from live_engine import (
        CURRENT_STATE_EDGE_DANGER_THRESHOLD,
        CURRENT_STATE_EDGE_STRONG_THRESHOLD,
    )
    if edge is None:
        return "missing", "current_edge missing"
    if edge < CURRENT_STATE_EDGE_DANGER_THRESHOLD:
        return (
            "current_edge_lt_0p03",
            f"current_edge<{CURRENT_STATE_EDGE_DANGER_THRESHOLD:.3f}",
        )
    if edge < CURRENT_STATE_EDGE_STRONG_THRESHOLD:
        return (
            "current_edge_0p03_to_0p08",
            f"{CURRENT_STATE_EDGE_DANGER_THRESHOLD:.3f}<=current_edge<{CURRENT_STATE_EDGE_STRONG_THRESHOLD:.3f}",
        )
    return (
        "current_edge_gte_0p08",
        f"current_edge>={CURRENT_STATE_EDGE_STRONG_THRESHOLD:.3f}",
    )


def build_current_state_edge_band_diagnostics(
    engine: "LiveTradingEngine",
) -> Dict[str, Dict[str, object]]:
    """Summarize score-event order outcomes by current-state edge band.

    This is intentionally diagnostic-only. It helps audit whether weak
    current-state support is a repeatable danger zone before considering
    any future gate or EV-policy feature.
    """
    from live_engine import (
        CURRENT_STATE_EDGE_DANGER_THRESHOLD,
        CURRENT_STATE_EDGE_STRONG_THRESHOLD,
    )

    def _is_score_event_order(bet) -> bool:
        if not getattr(bet, "order_id", None):
            return False
        strategy = str(getattr(bet, "state_value_strategy", "") or "")
        if strategy == "score_event_transition":
            return True
        # Older rows may predate the explicit strategy field; include them
        # only if they carry score-event current-state diagnostics.
        return strategy == "" and getattr(bet, "current_state_value_edge", None) is not None

    bands: Dict[str, Dict[str, object]] = {}
    for bet in engine._bets:
        if not _is_score_event_order(bet):
            continue
        edge = _float_attr(bet, "current_state_value_edge")
        band, label = current_state_edge_band(engine, edge)
        row = bands.setdefault(
            band,
            {
                "label": label,
                "placed": 0,
                "filled": 0,
                "missed": 0,
                "open": 0,
                "filled_wins": 0,
                "filled_losses": 0,
                "signal_wins": 0,
                "signal_losses": 0,
                "filled_staked": 0.0,
                "filled_profit": 0.0,
                "reserved_on_misses": 0.0,
                "current_state_value_edges": [],
                "decision_asks": [],
                "phantom_risk_scores": [],
                "inferred_lifts": [],
            },
        )
        row["placed"] = int(row["placed"]) + 1

        if engine._is_bet_executable(bet):
            row["filled"] = int(row["filled"]) + 1
            if getattr(bet, "won", None) is True:
                row["filled_wins"] = int(row["filled_wins"]) + 1
            elif getattr(bet, "won", None) is False:
                row["filled_losses"] = int(row["filled_losses"]) + 1
            row["filled_staked"] = float(row["filled_staked"]) + engine._filled_notional(bet)
            row["filled_profit"] = float(row["filled_profit"]) + float(getattr(bet, "profit", 0.0) or 0.0)
        elif (
            not engine._is_bet_executable(bet)
            and getattr(bet, "order_status", "") in {"cancelled", "expired"}
        ):
            row["missed"] = int(row["missed"]) + 1
            row["reserved_on_misses"] = (
                float(row["reserved_on_misses"]) + float(getattr(bet, "stake", 0.0) or 0.0)
            )
        elif _is_exposure_counted_status(getattr(bet, "order_status", "")):
            row["open"] = int(row["open"]) + 1

        if getattr(bet, "won", None) is True:
            row["signal_wins"] = int(row["signal_wins"]) + 1
        elif getattr(bet, "won", None) is False:
            row["signal_losses"] = int(row["signal_losses"]) + 1

        for attr_name, list_name in (
            ("current_state_value_edge", "current_state_value_edges"),
            ("decision_ask", "decision_asks"),
            ("shadow_phantom_risk_score", "phantom_risk_scores"),
            ("shadow_fv_inferred_lift", "inferred_lifts"),
        ):
            value = _float_attr(bet, attr_name)
            if value is not None:
                row[list_name].append(value)

    ordered_names = [
        "current_edge_lt_0p03",
        "current_edge_0p03_to_0p08",
        "current_edge_gte_0p08",
        "missing",
    ]
    out: Dict[str, Dict[str, object]] = {}
    for name in ordered_names:
        row = bands.get(name)
        if row is None:
            label = (
                f"current_edge<{CURRENT_STATE_EDGE_DANGER_THRESHOLD:.3f}"
                if name == "current_edge_lt_0p03"
                else f"{CURRENT_STATE_EDGE_DANGER_THRESHOLD:.3f}<=current_edge<{CURRENT_STATE_EDGE_STRONG_THRESHOLD:.3f}"
                if name == "current_edge_0p03_to_0p08"
                else f"current_edge>={CURRENT_STATE_EDGE_STRONG_THRESHOLD:.3f}"
                if name == "current_edge_gte_0p08"
                else "current_edge missing"
            )
            row = {
                "label": label,
                "placed": 0,
                "filled": 0,
                "missed": 0,
                "open": 0,
                "filled_wins": 0,
                "filled_losses": 0,
                "signal_wins": 0,
                "signal_losses": 0,
                "filled_staked": 0.0,
                "filled_profit": 0.0,
                "reserved_on_misses": 0.0,
                "current_state_value_edges": [],
                "decision_asks": [],
                "phantom_risk_scores": [],
                "inferred_lifts": [],
            }
        placed = int(row["placed"])
        filled = int(row["filled"])
        filled_staked = float(row["filled_staked"])
        edge_values = list(row.pop("current_state_value_edges"))
        ask_values = list(row.pop("decision_asks"))
        phantom_values = list(row.pop("phantom_risk_scores"))
        lift_values = list(row.pop("inferred_lifts"))
        row["fill_rate"] = round(filled / placed, 4) if placed else None
        row["filled_roi"] = (
            round(float(row["filled_profit"]) / filled_staked, 4)
            if filled_staked > 0
            else None
        )
        row["filled_staked"] = round(filled_staked, 2)
        row["filled_profit"] = round(float(row["filled_profit"]), 2)
        row["reserved_on_misses"] = round(float(row["reserved_on_misses"]), 2)
        row["avg_current_state_value_edge"] = (
            round(sum(edge_values) / len(edge_values), 4) if edge_values else None
        )
        row["min_current_state_value_edge"] = round(min(edge_values), 4) if edge_values else None
        row["max_current_state_value_edge"] = round(max(edge_values), 4) if edge_values else None
        row["avg_decision_ask"] = round(sum(ask_values) / len(ask_values), 4) if ask_values else None
        row["avg_phantom_risk_score"] = (
            round(sum(phantom_values) / len(phantom_values), 4) if phantom_values else None
        )
        row["avg_inferred_lift"] = round(sum(lift_values) / len(lift_values), 4) if lift_values else None
        out[name] = row
    return out


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def log_shadow_order_diagnostics(
    engine: "LiveTradingEngine",
    diagnostics: Dict[str, Dict[str, object]],
) -> None:
    if bool(getattr(engine, "_shadow_order_summary_logged", False)):
        return
    if not engine._bets:
        return
    LOGGER.info(
        "=== SHADOW RISK ORDER SUMMARY (%s; diagnostics only, not gates) ===",
        engine.date_str,
    )
    for name, row in diagnostics.items():
        fill_rate = row.get("fill_rate")
        fill_rate_str = "n/a" if fill_rate is None else f"{float(fill_rate) * 100:.0f}%"
        LOGGER.info(
            "  %s (%s): placed=%d filled=%d missed=%d open=%d fill_rate=%s "
            "filled_W/L=%d/%d signal_W/L=%d/%d pnl=$%.2f reserved_missed=$%.2f",
            name,
            row.get("label"),
            row.get("placed", 0),
            row.get("filled", 0),
            row.get("missed", 0),
            row.get("open", 0),
            fill_rate_str,
            row.get("filled_wins", 0),
            row.get("filled_losses", 0),
            row.get("signal_wins", 0),
            row.get("signal_losses", 0),
            row.get("filled_profit", 0.0),
            row.get("reserved_on_misses", 0.0),
        )
    engine._shadow_order_summary_logged = True


def _log_outcome_row(prefix: str, name: str, row: Dict[str, object]) -> None:
    fill_rate = row.get("fill_rate")
    fill_rate_str = "n/a" if fill_rate is None else f"{float(fill_rate) * 100:.0f}%"
    roi = row.get("filled_roi")
    roi_str = "n/a" if roi is None else f"{float(roi) * 100:.0f}%"
    LOGGER.info(
        "  %s%s (%s): placed=%d filled=%d missed=%d open=%d fill_rate=%s "
        "filled_W/L=%d/%d signal_W/L=%d/%d pnl=$%.2f roi=%s "
        "avg_ask=%s avg_edge=%s avg_cur_edge=%s avg_phantom=%s avg_rn=%s",
        prefix,
        name,
        row.get("label"),
        row.get("placed", 0),
        row.get("filled", 0),
        row.get("missed", 0),
        row.get("open", 0),
        fill_rate_str,
        row.get("filled_wins", 0),
        row.get("filled_losses", 0),
        row.get("signal_wins", 0),
        row.get("signal_losses", 0),
        row.get("filled_profit", 0.0),
        roi_str,
        row.get("avg_decision_ask"),
        row.get("avg_edge"),
        row.get("avg_current_state_value_edge"),
        row.get("avg_phantom_risk_score"),
        row.get("avg_runs_needed"),
    )


def log_shadow_feature_diagnostics(
    engine: "LiveTradingEngine",
    diagnostics: Dict[str, Dict[str, object]],
) -> None:
    if bool(getattr(engine, "_shadow_feature_summary_logged", False)):
        return
    if not engine._bets:
        return
    regimes = diagnostics.get("regimes") or {}
    matrices = [
        ("current_phantom_combo", diagnostics.get("current_phantom_combo") or {}),
        ("inning_runs_needed_combo", diagnostics.get("inning_runs_needed_combo") or {}),
        ("bottom9_home_lead_context", diagnostics.get("bottom9_home_lead_context") or {}),
    ]
    if not any(int((row or {}).get("placed", 0) or 0) > 0 for row in regimes.values()) and not any(
        matrix for _name, matrix in matrices
    ):
        return
    LOGGER.info(
        "=== SHADOW FEATURE SUMMARY (%s; diagnostics only, not gates) ===",
        engine.date_str,
    )
    for name, row in regimes.items():
        _log_outcome_row("regime:", name, row)
    for section_name, matrix in matrices:
        for name, row in matrix.items():
            if int(row.get("placed", 0) or 0) > 0:
                _log_outcome_row(f"{section_name}:", name, row)
    engine._shadow_feature_summary_logged = True


def log_current_state_edge_band_diagnostics(
    engine: "LiveTradingEngine",
    diagnostics: Dict[str, Dict[str, object]],
) -> None:
    if bool(getattr(engine, "_current_state_edge_band_summary_logged", False)):
        return
    if not any(int(row.get("placed", 0) or 0) > 0 for row in diagnostics.values()):
        return
    LOGGER.info(
        "=== CURRENT-STATE EDGE BAND SUMMARY (%s; score-event trades; diagnostics only, not gates) ===",
        engine.date_str,
    )
    for name, row in diagnostics.items():
        fill_rate = row.get("fill_rate")
        fill_rate_str = "n/a" if fill_rate is None else f"{float(fill_rate) * 100:.0f}%"
        roi = row.get("filled_roi")
        roi_str = "n/a" if roi is None else f"{float(roi) * 100:.0f}%"
        LOGGER.info(
            "  %s (%s): placed=%d filled=%d missed=%d open=%d fill_rate=%s "
            "filled_W/L=%d/%d signal_W/L=%d/%d pnl=$%.2f roi=%s "
            "avg_edge=%s avg_ask=%s avg_phantom=%s",
            name,
            row.get("label"),
            row.get("placed", 0),
            row.get("filled", 0),
            row.get("missed", 0),
            row.get("open", 0),
            fill_rate_str,
            row.get("filled_wins", 0),
            row.get("filled_losses", 0),
            row.get("signal_wins", 0),
            row.get("signal_losses", 0),
            row.get("filled_profit", 0.0),
            roi_str,
            row.get("avg_current_state_value_edge"),
            row.get("avg_decision_ask"),
            row.get("avg_phantom_risk_score"),
        )
    engine._current_state_edge_band_summary_logged = True
