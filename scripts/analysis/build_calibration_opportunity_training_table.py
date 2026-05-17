#!/usr/bin/env python3
"""
Build a model-bearing calibration-opportunity training table.

This table is the broad, low-selection-bias companion to the placed-order
training table. It reads the compact calibration-opportunity stream emitted by
the live/paper candidate logger, joins score-confirmation diagnostics and final
game totals, and writes one row per model-bearing opportunity.

Inputs:
  data/live_trading/candidate_universe/*_calibration_opportunities.jsonl
  data/live_trading/candidate_universe/*_score_confirmations.jsonl
  data/paper_trading/candidate_universe/*_calibration_opportunities.jsonl
  data/paper_trading/candidate_universe/*_score_confirmations.jsonl
  data/games/regular/**/**.json

Outputs:
  data/analysis_output/calibration_opportunity_training/
    calibration_opportunity_training_table.jsonl
    calibration_opportunity_training_table.csv
    calibration_opportunity_training_manifest.json
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import math
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple


PROJECT_DIR = Path(__file__).resolve().parents[2]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from scripts.trading.model_families import (  # noqa: E402
    NO_SCORE_DRIFT,
    SCORE_EVENT_TRANSITION,
    infer_signal_model_family,
)
from scripts.trading.remaining_opportunity import (  # noqa: E402
    compute_remaining_opportunity_fields,
)
from scripts.trading.scoring_path_features import (  # noqa: E402
    SCORING_PATH_FIELD_KEYS,
    SCORING_PATH_MODEL_FIELD_KEYS,
)
from scripts.trading.shadow_diagnostic_features import (  # noqa: E402
    compute_shadow_diagnostic_fields,
)
from scripts.trading.stage1_support import (  # noqa: E402
    STAGE1_SUPPORT_SUFFIXES,
    stage1_support_diagnostics,
    stage1_support_diagnostics_from_values,
    stage1_support_field_names,
)
from scripts.trading.weather_client import (  # noqa: E402
    WEATHER_FEATURE_FIELD_KEYS,
    WEATHER_MODEL_FEATURE_FIELD_KEYS,
)


LOGGER = logging.getLogger("build_calibration_opportunity_training_table")

DEFAULT_OUTPUT_ROOT = PROJECT_DIR / "data" / "analysis_output" / "calibration_opportunity_training"
LIVE_CANDIDATES_ROOT = PROJECT_DIR / "data" / "live_trading" / "candidate_universe"
PAPER_CANDIDATES_ROOT = PROJECT_DIR / "data" / "paper_trading" / "candidate_universe"
GAMES_ROOT = PROJECT_DIR / "data" / "games" / "regular"
DEFAULT_OUTPUT_STEM = "calibration_opportunity_training_table"
DEFAULT_STAGE1_CACHE_PATH = PROJECT_DIR / "cache" / "mlb_ou_cache.json"

CALIBRATION_SUFFIX = "_calibration_opportunities.jsonl"
CANDIDATES_SUFFIX = "_candidates.jsonl"
SCORE_CONFIRMATION_SUFFIX = "_score_confirmations.jsonl"
OUTCOME_SUFFIX = "_outcomes.jsonl"


IDENTITY_COLUMNS = [
    "schema_version",
    "mode",
    "session_date",
    "candidate_id",
    "outcome_join_key",
    "ts",
    "recorded_at",
    "signal_ts_epoch",
    "game_pk",
    "away_abbrev",
    "home_abbrev",
    "line",
    "side",
    "signal_model_family",
    "state_value_strategy",
    "decision",
    "decision_reason",
    "gate_policy_version",
    "calibration_repeat_policy",
    "calibration_repeat_group_key",
    "calibration_repeat_group_size",
    "calibration_repeat_group_index",
    "calibration_row_weight",
]

MARKET_COMPLEMENT_COLUMNS = [
    "over_token_id",
    "under_token_id",
    "over_best_bid",
    "over_best_ask",
    "over_mid",
    "over_spread",
    "over_ltp",
    "over_book_source",
    "decision_mid",
    "under_pair_available",
    "under_best_bid",
    "under_best_ask",
    "under_mid",
    "under_spread",
    "under_ltp",
    "under_book_source",
    "over_under_ask_sum",
    "over_under_bid_sum",
    "over_under_mid_sum",
    "over_mid_no_vig",
    "under_mid_no_vig",
    "decision_market_mid_no_vig",
]

INFERENCE_PANEL_COLUMNS = [
    "inference_panel_runs_considered",
    "inference_panel_selected_rule",
    "inference_panel_selected_runs",
    *[
        f"inference_run{runs}_{suffix}"
        for runs in (1, 2, 3)
        for suffix in (
            "selected",
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
            *(f"support_{suffix}" for suffix in STAGE1_SUPPORT_SUFFIXES),
            "fallback_level",
            "fallback_label",
            "cell_key",
            "line_fallback_mode",
            "line_source_key",
            "empirical_line_fallback_mode",
            "empirical_line_source_key",
        )
    ],
]

SELECTED_INFERRED_STATE_COLUMNS = [
    "inferred_state_base_poisson",
    "inferred_state_base_empirical",
    "inferred_state_poisson_minus_empirical",
    "inferred_state_empirical_edge",
    "inferred_state_n",
    "inferred_state_n_samples",
    "inferred_state_weighted_n",
    "inferred_state_effective_n",
    *stage1_support_field_names("inferred_state"),
    "inferred_state_fallback_level",
    "inferred_state_fallback_label",
    "inferred_state_cell_key",
    "inferred_state_line_key_poisson",
    "inferred_state_line_key_empirical",
    "inferred_state_line_fallback_mode",
    "inferred_state_empirical_line_fallback_mode",
    "inferred_state_empirical_line_source_key",
    "inferred_state_empirical_line_source_key_low",
    "inferred_state_empirical_line_source_key_high",
    "inferred_state_used_fallback",
    "inferred_state_base_source",
]

SELECTED_INFERRED_STATE_CORE_COLUMNS = [
    "inferred_state_base_poisson",
    "inferred_state_base_empirical",
    "inferred_state_poisson_minus_empirical",
    "inferred_state_n",
    "inferred_state_n_samples",
    "inferred_state_effective_n_proxy",
    "inferred_state_stage1_trust_weight",
    "inferred_state_fallback_level",
]

DECISION_TIME_AUDIT_COLUMNS = [
    "inning",
    "inning_state",
    "outs",
    "runners_on",
    "away_score_before",
    "home_score_before",
    "current_total",
    "home_leading_late",
    "batting_team_is_home",
    "bottom9_available_if_needed",
    "expected_remaining_half_innings",
    "expected_remaining_pa_bucket",
    "home_skip_bottom9_risk",
    *SCORING_PATH_FIELD_KEYS,
    *WEATHER_FEATURE_FIELD_KEYS,
    *MARKET_COMPLEMENT_COLUMNS,
    "runs_needed",
    "lead_abs",
    "inferred_runs",
    "decision_ask",
    "best_bid",
    "spread",
    "ask_bucket",
    "edge_bucket",
    "runs_needed_bucket",
    "phantom_risk_band",
    "fair_value",
    "fair_value_raw",
    "fair_value_calibrated",
    "base_fair_value",
    *SELECTED_INFERRED_STATE_COLUMNS,
    "stage2_run_env_delta",
    "stage2_weather_source",
    "stage2_weather_model_usable",
    "team_offense_delta",
    "edge",
    "min_edge_effective",
    "model_market_logit_residual",
    "raw_model_market_logit_residual",
    "current_state_market_logit_residual",
    "after_event_market_logit_residual",
    "empirical_state_market_logit_residual",
    "model_market_mid_no_vig_logit_residual",
    "empirical_state_mid_no_vig_logit_residual",
    *INFERENCE_PANEL_COLUMNS,
    "current_state_value_base_poisson",
    "current_state_value_base_empirical",
    "current_state_value_line_key_poisson",
    "current_state_value_line_key_empirical",
    "current_state_value_used_fallback",
    "current_state_value_state_fallback_level",
    "current_state_value_state_fallback_label",
    "current_state_value_state_cell_key",
    "current_state_value_line_fallback_mode",
    "current_state_value_line_source_key",
    "current_state_value_empirical_line_fallback_mode",
    "current_state_value_empirical_line_source_key",
    *stage1_support_field_names("current_state_value"),
    "current_state_value_fv_raw",
    "current_state_value_stage2_run_env_delta",
    "current_state_value_team_offense_delta",
    "current_state_value_edge",
    "current_state_value_empirical_edge",
    "current_state_value_away_score",
    "current_state_value_home_score",
    "current_state_value_total",
    "shadow_fv_current_state",
    "shadow_fv_after_inferred_score",
    "shadow_fv_inferred_lift",
    "shadow_no_event_edge",
    "shadow_after_event_edge",
    "shadow_p_score_event_proxy",
    "shadow_phantom_risk_score",
    "shadow_phantom_risk_band",
    "shadow_risk_tags",
    "shadow_low_ask_high_edge",
    "shadow_runs_needed_exact_3p5",
    "shadow_current_state_edge_bucket",
    "shadow_phantom_risk_bucket",
    "shadow_current_phantom_combo_bucket",
    "shadow_inning_bucket",
    "shadow_inning_runs_needed_bucket",
    "shadow_bottom9_home_lead_context",
    "shadow_home_skip_bottom9_risk_bucket",
    "shadow_post_tr20_extreme_020_pass",
    "shadow_post_tr20_ask_ramp_v2_pass",
    "shadow_post_tr20_gate6_relax_enforce_pass",
    "shadow_post_tr20_combined_pass",
    "score_segment_key",
    "score_segment_age_secs",
    "score_segment_drawdown",
    "shadow_no_score_drift_trigger",
    "posted_limit",
    "hypothetical_limit_price",
    "execution_ask",
    "execution_bid",
    "execution_policy_current_limit_price",
    "execution_policy_current_limit_ev_if_filled_per_share",
    "execution_policy_limit_plus_1c_price",
    "execution_policy_limit_plus_1c_ev_if_filled_per_share",
    "execution_policy_limit_plus_2c_price",
    "execution_policy_limit_plus_2c_ev_if_filled_per_share",
    "execution_policy_taker_like_price",
    "execution_policy_taker_like_ev_if_filled_per_share",
]

SCORE_CONFIRMATION_COLUMNS = [
    "score_confirmation_available",
    "score_confirmation_status",
    "score_confirmation_latency_secs",
    "observed_away_score",
    "observed_home_score",
    "observed_total",
    "score_delta_away",
    "score_delta_home",
    "score_delta_total",
    "score_confirmed_within_10s",
    "score_confirmed_within_30s",
    "score_confirmed_within_60s",
]

LABEL_COLUMNS = [
    "split",
    "split_date_rank",
    "label_final_available",
    "label_score_confirmation_available",
    "final_away",
    "final_home",
    "final_total",
    "target_trade",
    "target_over_win",
    "target_taker_profit_units",
    "target_limit_profit_units",
    "target_score_changed_any",
    "target_score_confirmed_10s",
    "target_score_confirmed_30s",
    "target_score_confirmed_60s",
    "target_no_score_change_60s",
    "target_phantom_no_score_60s",
]

_WEATHER_AUDIT_FIELD_SET = set(WEATHER_FEATURE_FIELD_KEYS)
_SCORING_PATH_NON_MODEL_FIELD_SET = set(SCORING_PATH_FIELD_KEYS) - set(SCORING_PATH_MODEL_FIELD_KEYS)

MODEL_FEATURE_COLUMNS = [
    c for c in DECISION_TIME_AUDIT_COLUMNS
    if c not in _WEATHER_AUDIT_FIELD_SET and c not in _SCORING_PATH_NON_MODEL_FIELD_SET
] + list(WEATHER_MODEL_FEATURE_FIELD_KEYS)


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Build a training table from calibration-opportunity rows."
    )
    p.add_argument("--mode", choices=["live", "paper", "both"], default="live")
    p.add_argument("--min-date", type=str, default="", help="Inclusive source date.")
    p.add_argument("--max-date", type=str, default="", help="Inclusive source date.")
    p.add_argument(
        "--live-root",
        type=Path,
        default=LIVE_CANDIDATES_ROOT,
        help=f"Live candidate universe root (default: {LIVE_CANDIDATES_ROOT}).",
    )
    p.add_argument(
        "--paper-root",
        type=Path,
        default=PAPER_CANDIDATES_ROOT,
        help=f"Paper candidate universe root (default: {PAPER_CANDIDATES_ROOT}).",
    )
    p.add_argument(
        "--games-root",
        type=Path,
        default=GAMES_ROOT,
        help=f"Scraped game JSON root for final totals (default: {GAMES_ROOT}).",
    )
    p.add_argument(
        "--output-root",
        type=Path,
        default=DEFAULT_OUTPUT_ROOT,
        help=f"Output directory (default: {DEFAULT_OUTPUT_ROOT}).",
    )
    p.add_argument(
        "--output-stem",
        type=str,
        default=DEFAULT_OUTPUT_STEM,
        help=f"Output stem (default: {DEFAULT_OUTPUT_STEM}).",
    )
    p.add_argument("--val-frac", type=float, default=0.15)
    p.add_argument("--test-frac", type=float, default=0.15)
    p.add_argument(
        "--drop-unlabeled-final",
        action="store_true",
        help="Drop opportunities without final score labels.",
    )
    p.add_argument(
        "--disable-raw-candidate-backfill",
        action="store_true",
        help=(
            "Do not backfill missing selected inferred-state fields from raw "
            "*_candidates.jsonl rows. Default keeps historical calibration "
            "sidecars usable after schema additions."
        ),
    )
    p.add_argument(
        "--stage1-cache-path",
        type=Path,
        default=DEFAULT_STAGE1_CACHE_PATH,
        help=(
            "Stage-1 cache used only to backfill support/trust diagnostics from "
            "logged state-cell keys (default: cache/mlb_ou_cache.json)."
        ),
    )
    p.add_argument(
        "--disable-stage1-support-backfill",
        action="store_true",
        help="Do not backfill Stage-1 support diagnostics from cached state-cell keys.",
    )
    p.add_argument("--strict", action="store_true", help="Fail on empty/invalid output.")
    p.add_argument(
        "--score-event-repeat-policy",
        choices=["dedupe", "weight", "none"],
        default="dedupe",
        help=(
            "How to handle repeated score-event opportunities with the same "
            "(game,line,inning,half,outs,runners,score,reason) state before "
            "training output is written. Default: dedupe."
        ),
    )
    p.add_argument("--verbose", action="store_true", help="Verbose logging.")
    return p.parse_args(argv)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _safe_float(v: Any) -> Optional[float]:
    try:
        if v is None or v == "":
            return None
        out = float(v)
    except Exception:
        return None
    if not math.isfinite(out):
        return None
    return out


def _safe_int(v: Any) -> Optional[int]:
    try:
        if v is None or v == "":
            return None
        return int(float(v))
    except Exception:
        return None


def _bool_to_int(v: Any) -> Optional[int]:
    if v is None or v == "":
        return None
    if isinstance(v, bool):
        return 1 if v else 0
    if isinstance(v, (int, float)):
        return 1 if bool(v) else 0
    text = str(v).strip().lower()
    if text in {"true", "1", "yes", "y"}:
        return 1
    if text in {"false", "0", "no", "n"}:
        return 0
    return None


def _date_in_range(date_str: str, min_date: Optional[str], max_date: Optional[str]) -> bool:
    if not date_str:
        return True
    if min_date and date_str < min_date:
        return False
    if max_date and date_str > max_date:
        return False
    return True


def _session_date_from_path(path: Path, suffix: str) -> Optional[str]:
    name = path.name
    if not name.endswith(suffix):
        return None
    date_str = name[: -len(suffix)]
    if len(date_str) == 10 and date_str[4] == "-" and date_str[7] == "-":
        return date_str
    return None


def _iter_source_files(root: Path, suffix: str) -> Iterable[Tuple[str, Path]]:
    if not root.exists():
        return
    for path in sorted(root.glob(f"*{suffix}")):
        session_date = _session_date_from_path(path, suffix)
        if session_date:
            yield session_date, path


def _read_jsonl(path: Path, warnings: List[str]) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    with open(path, encoding="utf-8") as f:
        for line_no, raw in enumerate(f, start=1):
            raw = raw.strip()
            if not raw:
                continue
            try:
                row = json.loads(raw)
            except Exception as exc:
                warnings.append(f"malformed JSON {path}:{line_no}: {exc}")
                continue
            if not isinstance(row, dict):
                warnings.append(f"non-dict JSON row {path}:{line_no}")
                continue
            rows.append(row)
    return rows


def _load_opportunity_rows(
    *,
    mode: str,
    root: Path,
    min_date: Optional[str],
    max_date: Optional[str],
    warnings: List[str],
) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for session_date, path in _iter_source_files(root, CALIBRATION_SUFFIX):
        if not _date_in_range(session_date, min_date, max_date):
            continue
        for row in _read_jsonl(path, warnings):
            out = dict(row)
            out["mode"] = str(out.get("mode") or mode)
            out["session_date"] = str(out.get("session_date") or session_date)
            out["source_path"] = str(path)
            rows.append(out)
    return rows


def _missing_value(value: Any) -> bool:
    return value is None or value == ""


def _copy_if_missing(row: Dict[str, Any], source: Mapping[str, Any], key: str) -> bool:
    if not _missing_value(row.get(key)):
        return False
    value = source.get(key)
    if _missing_value(value):
        return False
    row[key] = value
    return True


def _selected_panel_runs(row: Mapping[str, Any]) -> Optional[int]:
    selected = _safe_int(row.get("inference_panel_selected_runs"))
    if selected in (1, 2, 3):
        return selected
    return None


def _derive_selected_inferred_state_fields(row: Dict[str, Any]) -> int:
    """Populate selected inferred-state summary from the +1/+2/+3 panel."""
    selected = _selected_panel_runs(row)
    if selected is None:
        return 0
    prefix = f"inference_run{selected}"
    derived = 0

    mapping = {
        "inferred_state_base_poisson": f"{prefix}_base_poisson",
        "inferred_state_base_empirical": f"{prefix}_base_empirical",
        "inferred_state_poisson_minus_empirical": f"{prefix}_poisson_minus_empirical",
        "inferred_state_n": f"{prefix}_n",
        "inferred_state_n_samples": f"{prefix}_n_samples",
        "inferred_state_effective_n": f"{prefix}_effective_n",
        "inferred_state_effective_n_proxy": f"{prefix}_support_effective_n_proxy",
        "inferred_state_stage1_trust_weight": f"{prefix}_support_stage1_trust_weight",
        "inferred_state_stage1_support_bucket": f"{prefix}_support_stage1_support_bucket",
        "inferred_state_exact_cell_support": f"{prefix}_support_exact_cell_support",
        "inferred_state_poisson_line_exact": f"{prefix}_support_poisson_line_exact",
        "inferred_state_empirical_line_exact": f"{prefix}_support_empirical_line_exact",
        "inferred_state_empirical_sample_support": f"{prefix}_support_empirical_sample_support",
        "inferred_state_empirical_sample_bucket": f"{prefix}_support_empirical_sample_bucket",
        "inferred_state_state_fallback_penalty": f"{prefix}_support_state_fallback_penalty",
        "inferred_state_line_fallback_penalty": f"{prefix}_support_line_fallback_penalty",
        "inferred_state_fallback_level": f"{prefix}_fallback_level",
        "inferred_state_fallback_label": f"{prefix}_fallback_label",
        "inferred_state_cell_key": f"{prefix}_cell_key",
        "inferred_state_line_key_poisson": f"{prefix}_line_source_key",
        "inferred_state_line_fallback_mode": f"{prefix}_line_fallback_mode",
        "inferred_state_line_key_empirical": f"{prefix}_empirical_line_source_key",
        "inferred_state_empirical_line_fallback_mode": f"{prefix}_empirical_line_fallback_mode",
        "inferred_state_empirical_line_source_key": f"{prefix}_empirical_line_source_key",
    }
    for target, source in mapping.items():
        if _missing_value(row.get(target)) and not _missing_value(row.get(source)):
            row[target] = row.get(source)
            derived += 1

    if _missing_value(row.get("inferred_state_empirical_edge")):
        empirical = _safe_float(row.get("inferred_state_base_empirical"))
        ask = _safe_float(row.get("decision_ask"))
        if empirical is not None and ask is not None:
            row["inferred_state_empirical_edge"] = empirical - ask
            derived += 1
    if _missing_value(row.get("inferred_state_used_fallback")):
        fallback_level = _safe_int(row.get("inferred_state_fallback_level"))
        if fallback_level is not None:
            row["inferred_state_used_fallback"] = bool(fallback_level > 0)
            derived += 1
    if _missing_value(row.get("inferred_state_base_source")) and not _missing_value(
        row.get("inferred_state_base_poisson")
    ):
        row["inferred_state_base_source"] = "poisson_runtime"
        derived += 1
    if _missing_value(row.get("inferred_state_effective_n_proxy")):
        support = row.get("inferred_state_effective_n")
        if _missing_value(support):
            support = row.get("inferred_state_weighted_n")
        if _missing_value(support):
            support = row.get("inferred_state_n")
        support_diag = stage1_support_diagnostics_from_values(
            support_mass=support,
            empirical_sample_support=row.get("inferred_state_n_samples"),
            state_fallback_level=row.get("inferred_state_fallback_level"),
            poisson_line_fallback_mode=row.get("inferred_state_line_fallback_mode"),
            empirical_line_fallback_mode=row.get("inferred_state_empirical_line_fallback_mode"),
        )
        for suffix, value in support_diag.items():
            key = f"inferred_state_{suffix}"
            if _missing_value(row.get(key)) and not _missing_value(value):
                row[key] = value
                derived += 1
    return derived


def _derive_inferred_support_from_selected(row: Dict[str, Any]) -> int:
    """Backfill Stage-1 support proxy from selected inferred-state summary fields."""
    if not _missing_value(row.get("inferred_state_effective_n_proxy")):
        return 0
    support = row.get("inferred_state_effective_n")
    if _missing_value(support):
        support = row.get("inferred_state_weighted_n")
    if _missing_value(support):
        support = row.get("inferred_state_n")
    support_diag = stage1_support_diagnostics_from_values(
        support_mass=support,
        empirical_sample_support=row.get("inferred_state_n_samples"),
        state_fallback_level=row.get("inferred_state_fallback_level"),
        poisson_line_fallback_mode=row.get("inferred_state_line_fallback_mode"),
        empirical_line_fallback_mode=row.get("inferred_state_empirical_line_fallback_mode"),
    )
    derived = 0
    for suffix, value in support_diag.items():
        key = f"inferred_state_{suffix}"
        if _missing_value(row.get(key)) and not _missing_value(value):
            row[key] = value
            derived += 1
    return derived


def _raw_candidate_backfill_needs(rows: Sequence[Dict[str, Any]]) -> Tuple[set[str], set[str]]:
    needed_ids: set[str] = set()
    needed_dates: set[str] = set()
    for row in rows:
        candidate_id = str(row.get("candidate_id") or "")
        session_date = str(row.get("session_date") or "")[:10]
        if not candidate_id or not session_date:
            continue
        has_partial_summary = any(
            not _missing_value(row.get(key)) for key in SELECTED_INFERRED_STATE_COLUMNS
        )
        if not has_partial_summary:
            continue
        if any(_missing_value(row.get(key)) for key in SELECTED_INFERRED_STATE_CORE_COLUMNS):
            needed_ids.add(candidate_id)
            needed_dates.add(session_date)
    return needed_ids, needed_dates


def _merge_backfill_stats(
    base: Mapping[str, int],
    extra: Mapping[str, int],
) -> Dict[str, int]:
    keys = set(base) | set(extra)
    out: Dict[str, int] = {}
    for key in keys:
        if key in {"rows_seen", "rows_with_selected_summary_after"}:
            out[key] = int(extra.get(key, base.get(key, 0)) or 0)
        else:
            out[key] = int(base.get(key, 0) or 0) + int(extra.get(key, 0) or 0)
    return out


def _load_raw_candidate_backfill(
    *,
    mode: str,
    root: Path,
    needed_candidate_ids: set[str],
    needed_dates: set[str],
    warnings: List[str],
) -> Dict[str, Dict[str, Any]]:
    if not needed_candidate_ids or not needed_dates:
        return {}
    backfill: Dict[str, Dict[str, Any]] = {}
    for session_date, path in _iter_source_files(root, CANDIDATES_SUFFIX):
        if session_date not in needed_dates:
            continue
        for row in _read_jsonl(path, warnings):
            candidate_id = str(row.get("candidate_id") or "")
            if candidate_id not in needed_candidate_ids:
                continue
            selected = {
                key: row.get(key)
                for key in SELECTED_INFERRED_STATE_COLUMNS
                if not _missing_value(row.get(key))
            }
            if not selected:
                continue
            selected["candidate_id"] = candidate_id
            selected["mode"] = str(row.get("mode") or mode)
            selected["session_date"] = str(row.get("session_date") or session_date)
            backfill[candidate_id] = selected
    return backfill


def _load_stage1_cache_cells(path: Path, warnings: List[str]) -> Dict[str, Mapping[str, Any]]:
    if not path:
        return {}
    if not path.exists():
        warnings.append(f"Stage-1 support backfill cache not found: {path}")
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except Exception as exc:
        warnings.append(f"failed to parse Stage-1 support cache {path}: {exc}")
        return {}
    cells = data.get("cells") if isinstance(data, dict) else None
    if not isinstance(cells, dict):
        warnings.append(f"Stage-1 support cache has no cells mapping: {path}")
        return {}
    return {str(k): v for k, v in cells.items() if isinstance(v, Mapping)}


def _line_key_exact_mode_from_cell(
    *,
    row: Mapping[str, Any],
    cell: Mapping[str, Any],
    key_name: str,
    fallback_mode_name: str,
) -> Any:
    mode = row.get(fallback_mode_name)
    if not _missing_value(mode):
        return mode
    key = row.get(key_name)
    if not _missing_value(key) and str(key) in cell:
        return "exact"
    return mode


def _backfill_stage1_support_for_prefix(
    row: Dict[str, Any],
    *,
    prefix: str,
    cells: Mapping[str, Mapping[str, Any]],
    cell_key_name: str,
    state_fallback_level_name: str,
    poisson_line_mode_name: str,
    empirical_line_mode_name: str,
    poisson_line_key_name: str,
    empirical_line_key_name: str,
) -> int:
    cell_key = row.get(cell_key_name)
    if _missing_value(cell_key):
        return 0
    cell = cells.get(str(cell_key))
    if not isinstance(cell, Mapping):
        return 0
    poisson_mode = _line_key_exact_mode_from_cell(
        row=row,
        cell=cell,
        key_name=poisson_line_key_name,
        fallback_mode_name=poisson_line_mode_name,
    )
    empirical_mode = _line_key_exact_mode_from_cell(
        row=row,
        cell=cell,
        key_name=empirical_line_key_name,
        fallback_mode_name=empirical_line_mode_name,
    )
    support_diag = stage1_support_diagnostics(
        cell=cell,
        state_fallback_level=row.get(state_fallback_level_name),
        poisson_line_fallback_mode=poisson_mode,
        empirical_line_fallback_mode=empirical_mode,
    )
    copied = 0
    for suffix, value in support_diag.items():
        key = f"{prefix}_{suffix}"
        if _missing_value(row.get(key)) and not _missing_value(value):
            row[key] = value
            copied += 1
    return copied


def backfill_stage1_support_fields(
    rows: Sequence[Dict[str, Any]],
    *,
    cells: Mapping[str, Mapping[str, Any]],
) -> Dict[str, int]:
    stats = {
        "rows_seen": len(rows),
        "cells_loaded": len(cells),
        "rows_with_inferred_support_backfill": 0,
        "fields_inferred_support_backfilled": 0,
        "rows_with_current_support_backfill": 0,
        "fields_current_support_backfilled": 0,
    }
    if not cells:
        return stats
    for row in rows:
        inferred = _backfill_stage1_support_for_prefix(
            row,
            prefix="inferred_state",
            cells=cells,
            cell_key_name="inferred_state_cell_key",
            state_fallback_level_name="inferred_state_fallback_level",
            poisson_line_mode_name="inferred_state_line_fallback_mode",
            empirical_line_mode_name="inferred_state_empirical_line_fallback_mode",
            poisson_line_key_name="inferred_state_line_key_poisson",
            empirical_line_key_name="inferred_state_empirical_line_source_key",
        )
        if inferred:
            stats["rows_with_inferred_support_backfill"] += 1
            stats["fields_inferred_support_backfilled"] += inferred
        current = _backfill_stage1_support_for_prefix(
            row,
            prefix="current_state_value",
            cells=cells,
            cell_key_name="current_state_value_state_cell_key",
            state_fallback_level_name="current_state_value_state_fallback_level",
            poisson_line_mode_name="current_state_value_line_fallback_mode",
            empirical_line_mode_name="current_state_value_empirical_line_fallback_mode",
            poisson_line_key_name="current_state_value_line_source_key",
            empirical_line_key_name="current_state_value_line_key_empirical",
        )
        if current:
            stats["rows_with_current_support_backfill"] += 1
            stats["fields_current_support_backfilled"] += current
    return stats


def backfill_selected_inferred_state_fields(
    rows: Sequence[Dict[str, Any]],
    *,
    raw_backfill_by_candidate_id: Mapping[str, Mapping[str, Any]],
) -> Dict[str, int]:
    stats = {
        "rows_seen": len(rows),
        "raw_candidate_rows_matched": 0,
        "rows_with_raw_backfill": 0,
        "fields_backfilled_from_raw": 0,
        "rows_with_panel_derivation": 0,
        "fields_derived_from_panel": 0,
        "rows_with_selected_summary_after": 0,
    }
    for row in rows:
        candidate_id = str(row.get("candidate_id") or "")
        raw = raw_backfill_by_candidate_id.get(candidate_id)
        copied = 0
        if raw:
            stats["raw_candidate_rows_matched"] += 1
            for key in SELECTED_INFERRED_STATE_COLUMNS:
                if _copy_if_missing(row, raw, key):
                    copied += 1
        if copied:
            stats["rows_with_raw_backfill"] += 1
            stats["fields_backfilled_from_raw"] += copied

        derived = _derive_selected_inferred_state_fields(row)
        derived += _derive_inferred_support_from_selected(row)
        if derived:
            stats["rows_with_panel_derivation"] += 1
            stats["fields_derived_from_panel"] += derived

        if (
            not _missing_value(row.get("inferred_state_base_poisson"))
            and not _missing_value(row.get("inferred_state_base_empirical"))
        ):
            stats["rows_with_selected_summary_after"] += 1
    return stats


def _load_score_confirmations(
    *,
    mode: str,
    root: Path,
    min_date: Optional[str],
    max_date: Optional[str],
    warnings: List[str],
) -> Dict[str, Dict[str, Any]]:
    by_candidate_id: Dict[str, Dict[str, Any]] = {}
    for session_date, path in _iter_source_files(root, SCORE_CONFIRMATION_SUFFIX):
        if not _date_in_range(session_date, min_date, max_date):
            continue
        for row in _read_jsonl(path, warnings):
            candidate_id = str(row.get("candidate_id") or "")
            if not candidate_id:
                warnings.append(f"[{mode}] score confirmation missing candidate_id in {path}")
                continue
            out = dict(row)
            out["mode"] = str(out.get("mode") or mode)
            out["session_date"] = str(out.get("session_date") or session_date)
            if candidate_id in by_candidate_id:
                warnings.append(f"[{mode}] duplicate score confirmation candidate_id={candidate_id}")
            by_candidate_id[candidate_id] = out
    return by_candidate_id


def _load_outcome_final_scores(
    *,
    mode: str,
    root: Path,
    min_date: Optional[str],
    max_date: Optional[str],
    warnings: List[str],
) -> Dict[int, Dict[str, int]]:
    finals: Dict[int, Dict[str, int]] = {}
    for session_date, path in _iter_source_files(root, OUTCOME_SUFFIX):
        if not _date_in_range(session_date, min_date, max_date):
            continue
        for row in _read_jsonl(path, warnings):
            game_pk = _safe_int(row.get("game_pk"))
            away = _safe_int(row.get("final_away"))
            home = _safe_int(row.get("final_home"))
            total = _safe_int(row.get("final_total"))
            if game_pk is None:
                warnings.append(f"[{mode}] outcome row missing game_pk in {path}")
                continue
            if total is None and away is not None and home is not None:
                total = away + home
            if total is None:
                warnings.append(f"[{mode}] outcome row missing final_total for game_pk={game_pk}")
                continue
            finals[game_pk] = {
                "final_away": away if away is not None else 0,
                "final_home": home if home is not None else 0,
                "final_total": total,
            }
    return finals


def _load_final_scores(games_root: Path, warnings: List[str]) -> Dict[int, Dict[str, int]]:
    finals: Dict[int, Dict[str, int]] = {}
    if not games_root.exists():
        warnings.append(f"games root does not exist: {games_root}")
        return finals
    for game_path in sorted(games_root.glob("*/*/*.json")):
        try:
            with open(game_path, encoding="utf-8") as f:
                game = json.load(f)
        except Exception as exc:
            warnings.append(f"failed to parse game file {game_path}: {exc}")
            continue
        game_pk = _safe_int(game.get("gamePk"))
        if game_pk is None:
            continue
        teams = game.get("liveData", {}).get("linescore", {}).get("teams", {})
        away = _safe_int(teams.get("away", {}).get("runs"))
        home = _safe_int(teams.get("home", {}).get("runs"))
        if away is None or home is None:
            continue
        finals[game_pk] = {
            "final_away": away,
            "final_home": home,
            "final_total": away + home,
        }
    return finals


def _allocate_split_counts(num_dates: int, val_frac: float, test_frac: float) -> Tuple[int, int, int]:
    if num_dates <= 0:
        return 0, 0, 0
    if num_dates == 1:
        return 1, 0, 0
    if num_dates == 2:
        return 1, 0, 1

    val_n = max(1, int(round(num_dates * val_frac)))
    test_n = max(1, int(round(num_dates * test_frac)))
    train_n = num_dates - val_n - test_n
    while train_n < 1 and (val_n > 1 or test_n > 1):
        if val_n >= test_n and val_n > 1:
            val_n -= 1
        elif test_n > 1:
            test_n -= 1
        train_n = num_dates - val_n - test_n
    if train_n < 1:
        train_n = 1
        remaining = num_dates - train_n
        val_n = 1 if remaining >= 2 else 0
        test_n = remaining - val_n
    return train_n, val_n, test_n


def _build_date_split_map(
    dates: Sequence[str],
    *,
    val_frac: float,
    test_frac: float,
) -> Tuple[Dict[str, str], Dict[str, int], Dict[str, List[str]]]:
    unique_dates = sorted(set(d for d in dates if d))
    train_n, val_n, test_n = _allocate_split_counts(
        len(unique_dates), val_frac=val_frac, test_frac=test_frac
    )
    train_dates = unique_dates[:train_n]
    val_dates = unique_dates[train_n : train_n + val_n]
    test_dates = unique_dates[train_n + val_n : train_n + val_n + test_n]
    split_map: Dict[str, str] = {}
    for d in train_dates:
        split_map[d] = "train"
    for d in val_dates:
        split_map[d] = "validation"
    for d in test_dates:
        split_map[d] = "test"
    return (
        split_map,
        {d: i for i, d in enumerate(unique_dates)},
        {"train": train_dates, "validation": val_dates, "test": test_dates},
    )


def _first_float(row: Mapping[str, Any], keys: Sequence[str]) -> Optional[float]:
    for key in keys:
        value = _safe_float(row.get(key))
        if value is not None:
            return value
    return None


def _profit_units_for_price(*, won: Optional[int], price: Optional[float]) -> Optional[float]:
    if won is None or price is None or price <= 0:
        return None
    return (1.0 / price - 1.0) if won == 1 else -1.0


def _derive_labels(
    row: Dict[str, Any],
    confirmation: Optional[Dict[str, Any]],
    final_score: Optional[Dict[str, int]],
) -> Dict[str, Any]:
    labels: Dict[str, Any] = {}
    decision = str(row.get("decision") or "").lower()
    labels["target_trade"] = 1 if decision == "trade" else 0

    line = _safe_float(row.get("line"))
    labels["label_final_available"] = bool(final_score and line is not None)
    labels["final_away"] = final_score.get("final_away") if final_score else None
    labels["final_home"] = final_score.get("final_home") if final_score else None
    labels["final_total"] = final_score.get("final_total") if final_score else None

    target_over_win: Optional[int] = None
    if final_score and line is not None:
        target_over_win = 1 if final_score["final_total"] > line else 0
    labels["target_over_win"] = target_over_win
    labels["target_taker_profit_units"] = _profit_units_for_price(
        won=target_over_win,
        price=_safe_float(row.get("decision_ask")),
    )
    labels["target_limit_profit_units"] = _profit_units_for_price(
        won=target_over_win,
        price=_first_float(
            row,
            (
                "execution_policy_current_limit_price",
                "hypothetical_limit_price",
                "posted_limit",
                "decision_ask",
            ),
        ),
    )

    if confirmation:
        labels["label_score_confirmation_available"] = True
        labels["score_confirmation_available"] = True
        labels["score_confirmation_status"] = confirmation.get("confirmation_status")
        labels["score_confirmation_latency_secs"] = confirmation.get(
            "score_confirmation_latency_secs"
        )
        for c in (
            "observed_away_score",
            "observed_home_score",
            "observed_total",
            "score_delta_away",
            "score_delta_home",
            "score_delta_total",
            "score_confirmed_within_10s",
            "score_confirmed_within_30s",
            "score_confirmed_within_60s",
        ):
            labels[c] = confirmation.get(c)
        confirmed_10 = _bool_to_int(confirmation.get("score_confirmed_within_10s"))
        confirmed_30 = _bool_to_int(confirmation.get("score_confirmed_within_30s"))
        confirmed_60 = _bool_to_int(confirmation.get("score_confirmed_within_60s"))
        status = str(confirmation.get("confirmation_status") or "")
        labels["target_score_changed_any"] = 1 if status == "score_changed" else 0
        labels["target_score_confirmed_10s"] = confirmed_10
        labels["target_score_confirmed_30s"] = confirmed_30
        labels["target_score_confirmed_60s"] = confirmed_60
        labels["target_no_score_change_60s"] = 1 - confirmed_60 if confirmed_60 is not None else None
    else:
        labels["label_score_confirmation_available"] = False
        labels["score_confirmation_available"] = False
        for c in SCORE_CONFIRMATION_COLUMNS[1:]:
            labels[c] = None
        labels["target_score_changed_any"] = None
        labels["target_score_confirmed_10s"] = None
        labels["target_score_confirmed_30s"] = None
        labels["target_score_confirmed_60s"] = None
        labels["target_no_score_change_60s"] = None

    family = infer_signal_model_family(row)
    if family == SCORE_EVENT_TRANSITION and labels["target_no_score_change_60s"] is not None:
        labels["target_phantom_no_score_60s"] = labels["target_no_score_change_60s"]
    else:
        labels["target_phantom_no_score_60s"] = None
    return labels


def _enrich_opportunity_row(
    row: Dict[str, Any],
    *,
    confirmation: Optional[Dict[str, Any]],
    final_score: Optional[Dict[str, int]],
    split: str,
    split_date_rank: Optional[int],
) -> Dict[str, Any]:
    out = dict(row)
    out["signal_model_family"] = infer_signal_model_family(out)
    for field, value in compute_remaining_opportunity_fields(
        away_score=out.get("away_score_before"),
        home_score=out.get("home_score_before"),
        inning=out.get("inning"),
        inning_state=out.get("inning_state"),
    ).items():
        if out.get(field) in (None, ""):
            out[field] = value
    for field, value in compute_shadow_diagnostic_fields(out).items():
        if out.get(field) in (None, ""):
            out[field] = value
    out["split"] = split
    out["split_date_rank"] = split_date_rank
    out.update(_derive_labels(out, confirmation=confirmation, final_score=final_score))
    return out


def build_training_rows(
    rows: Sequence[Dict[str, Any]],
    *,
    confirmations_by_candidate_id: Mapping[str, Dict[str, Any]],
    final_scores_by_game_pk: Mapping[int, Dict[str, int]],
    split_map: Mapping[str, str],
    date_rank: Mapping[str, int],
    drop_unlabeled_final: bool = False,
) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for row in rows:
        session_date = str(row.get("session_date") or "")
        game_pk = _safe_int(row.get("game_pk"))
        candidate_id = str(row.get("candidate_id") or "")
        final_score = final_scores_by_game_pk.get(game_pk) if game_pk is not None else None
        if drop_unlabeled_final and not final_score:
            continue
        out.append(
            _enrich_opportunity_row(
                dict(row),
                confirmation=confirmations_by_candidate_id.get(candidate_id),
                final_score=final_score,
                split=split_map.get(session_date, "train"),
                split_date_rank=date_rank.get(session_date),
            )
        )
    out.sort(
        key=lambda r: (
            str(r.get("session_date") or ""),
            int(r.get("split_date_rank") or 0),
            str(r.get("mode") or ""),
            str(r.get("candidate_id") or ""),
            str(r.get("signal_ts_epoch") or r.get("ts") or ""),
        )
    )
    return out


def _repeat_key_value(row: Mapping[str, Any], field: str) -> str:
    value = row.get(field)
    if value is None:
        return ""
    if isinstance(value, float):
        return f"{value:.3f}"
    return str(value)


def score_event_repeat_group_key(row: Mapping[str, Any]) -> Optional[str]:
    if str(row.get("signal_model_family") or "") != SCORE_EVENT_TRANSITION:
        return None
    parts = [
        _repeat_key_value(row, "mode"),
        _repeat_key_value(row, "game_pk"),
        _repeat_key_value(row, "line"),
        _repeat_key_value(row, "inning"),
        _repeat_key_value(row, "inning_state"),
        _repeat_key_value(row, "outs"),
        _repeat_key_value(row, "runners_on"),
        _repeat_key_value(row, "away_score_before"),
        _repeat_key_value(row, "home_score_before"),
        _repeat_key_value(row, "current_total"),
        _repeat_key_value(row, "decision_reason"),
    ]
    return json.dumps(parts, separators=(",", ":"))


def _repeat_sort_key(index: int, row: Mapping[str, Any]) -> Tuple[float, str, int]:
    ts = _safe_float(row.get("signal_ts_epoch"))
    if ts is None:
        ts = _safe_float(row.get("ts"))
    if ts is None:
        ts = float(index)
    return (ts, str(row.get("candidate_id") or ""), index)


def apply_score_event_repeat_policy(
    rows: Sequence[Dict[str, Any]],
    *,
    policy: str = "dedupe",
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Reduce repeated score-event polling evidence before model training.

    The grouping key intentionally ignores price/edge so repeated book polling
    of the same baseball state contributes one evidence unit unless the caller
    chooses the explicit weighting/none modes.
    """
    if policy not in {"dedupe", "weight", "none"}:
        raise ValueError(f"unknown score-event repeat policy: {policy}")

    groups: Dict[str, List[Tuple[int, Dict[str, Any]]]] = defaultdict(list)
    passthrough: List[Tuple[int, Dict[str, Any]]] = []
    for index, row in enumerate(rows):
        key = score_event_repeat_group_key(row)
        if key is None:
            out = dict(row)
            out.setdefault("calibration_repeat_policy", "not_applicable")
            out.setdefault("calibration_repeat_group_key", None)
            out.setdefault("calibration_repeat_group_size", 1)
            out.setdefault("calibration_repeat_group_index", 1)
            out.setdefault("calibration_row_weight", 1.0)
            passthrough.append((index, out))
        else:
            groups[key].append((index, dict(row)))

    emitted: List[Tuple[int, Dict[str, Any]]] = list(passthrough)
    collapsed_rows = 0
    max_group_size = 0
    repeated_groups = 0
    for key, items in groups.items():
        sorted_items = sorted(items, key=lambda item: _repeat_sort_key(item[0], item[1]))
        group_size = len(sorted_items)
        max_group_size = max(max_group_size, group_size)
        if group_size > 1:
            repeated_groups += 1

        if policy == "dedupe":
            index, row = sorted_items[0]
            row["calibration_repeat_policy"] = policy
            row["calibration_repeat_group_key"] = key
            row["calibration_repeat_group_size"] = group_size
            row["calibration_repeat_group_index"] = 1
            row["calibration_row_weight"] = 1.0
            emitted.append((index, row))
            collapsed_rows += max(0, group_size - 1)
            continue

        weight = 1.0 / float(group_size) if policy == "weight" and group_size else 1.0
        for group_index, (index, row) in enumerate(sorted_items, start=1):
            row["calibration_repeat_policy"] = policy
            row["calibration_repeat_group_key"] = key
            row["calibration_repeat_group_size"] = group_size
            row["calibration_repeat_group_index"] = group_index
            row["calibration_row_weight"] = weight
            emitted.append((index, row))

    emitted.sort(key=lambda item: item[0])
    output_rows = [row for _, row in emitted]
    stats = {
        "policy": policy,
        "input_rows": len(rows),
        "output_rows": len(output_rows),
        "score_event_groups": len(groups),
        "score_event_repeated_groups": repeated_groups,
        "score_event_collapsed_rows": collapsed_rows,
        "score_event_max_group_size": max_group_size,
    }
    return output_rows, stats


def _dedupe_preserve_order(values: Iterable[str]) -> List[str]:
    seen = set()
    out: List[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        out.append(value)
    return out


def _infer_output_columns(rows: Sequence[Dict[str, Any]]) -> List[str]:
    source_columns = sorted({k for row in rows for k in row.keys()})
    preferred = (
        IDENTITY_COLUMNS
        + DECISION_TIME_AUDIT_COLUMNS
        + SCORE_CONFIRMATION_COLUMNS
        + LABEL_COLUMNS
        + ["source_path"]
    )
    return _dedupe_preserve_order(preferred + source_columns)


def _write_jsonl(path: Path, rows: Iterable[Dict[str, Any]], columns: Sequence[str]) -> None:
    with open(path, "w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps({c: row.get(c) for c in columns}) + "\n")


def _write_csv(path: Path, rows: Sequence[Dict[str, Any]], columns: Sequence[str]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(columns), extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def _counts(rows: Sequence[Dict[str, Any]], key: str) -> Dict[str, int]:
    counts: Dict[str, int] = defaultdict(int)
    for row in rows:
        counts[str(row.get(key) if row.get(key) is not None else "missing")] += 1
    return dict(sorted(counts.items()))


def _label_stats(rows: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    return {
        "rows": len(rows),
        "final_label_rows": sum(1 for r in rows if r.get("label_final_available")),
        "score_confirmation_rows": sum(
            1 for r in rows if r.get("label_score_confirmation_available")
        ),
        "target_over_wins": sum(1 for r in rows if r.get("target_over_win") == 1),
        "target_score_confirmed_60s": sum(
            1 for r in rows if r.get("target_score_confirmed_60s") == 1
        ),
        "target_no_score_change_60s": sum(
            1 for r in rows if r.get("target_no_score_change_60s") == 1
        ),
        "target_trade": sum(1 for r in rows if r.get("target_trade") == 1),
    }


def write_manifest(
    *,
    path: Path,
    args: argparse.Namespace,
    source_rows: Sequence[Dict[str, Any]],
    confirmations_by_candidate_id: Mapping[str, Dict[str, Any]],
    outcome_final_scores_by_game_pk: Mapping[int, Dict[str, int]],
    scraped_final_scores_by_game_pk: Mapping[int, Dict[str, int]],
    training_rows: Sequence[Dict[str, Any]],
    split_dates: Mapping[str, List[str]],
    output_columns: Sequence[str],
    family_output_paths: Mapping[str, Mapping[str, Any]],
    repeat_policy_stats: Mapping[str, Any],
    selected_inferred_state_backfill_stats: Mapping[str, Any],
    stage1_support_backfill_stats: Mapping[str, Any],
    warnings: Sequence[str],
) -> None:
    rows_by_split = {name: [r for r in training_rows if r.get("split") == name] for name in ("train", "validation", "test")}
    model_feature_columns = [c for c in MODEL_FEATURE_COLUMNS if c in output_columns]
    audit_columns = [c for c in DECISION_TIME_AUDIT_COLUMNS if c in output_columns]
    manifest = {
        "generated_at_utc": _now_iso(),
        "config": {
            "mode": args.mode,
            "min_date": args.min_date or None,
            "max_date": args.max_date or None,
            "live_root": str(args.live_root),
            "paper_root": str(args.paper_root),
            "games_root": str(args.games_root),
            "output_root": str(args.output_root),
            "output_stem": args.output_stem,
            "val_frac": args.val_frac,
            "test_frac": args.test_frac,
            "drop_unlabeled_final": bool(args.drop_unlabeled_final),
            "raw_candidate_backfill_enabled": not bool(args.disable_raw_candidate_backfill),
            "stage1_support_backfill_enabled": not bool(args.disable_stage1_support_backfill),
            "stage1_cache_path": str(args.stage1_cache_path) if args.stage1_cache_path else None,
            "score_event_repeat_policy": args.score_event_repeat_policy,
            "strict": bool(args.strict),
        },
        "counts": {
            "source_rows_total": len(source_rows),
            "training_rows_total": len(training_rows),
            "score_confirmations_loaded": len(confirmations_by_candidate_id),
            "outcome_final_game_labels_loaded": len(outcome_final_scores_by_game_pk),
            "scraped_final_game_labels_loaded": len(scraped_final_scores_by_game_pk),
            "rows_by_mode": _counts(training_rows, "mode"),
            "rows_by_family": _counts(training_rows, "signal_model_family"),
            "rows_by_decision": _counts(training_rows, "decision"),
            "rows_by_decision_reason": _counts(training_rows, "decision_reason"),
            "rows_by_split": {k: len(v) for k, v in rows_by_split.items()},
            "dates_by_split": dict(split_dates),
            "score_event_repeat_policy": dict(repeat_policy_stats),
            "selected_inferred_state_backfill": dict(selected_inferred_state_backfill_stats),
            "stage1_support_backfill": dict(stage1_support_backfill_stats),
        },
        "label_stats": {
            "overall": _label_stats(training_rows),
            "by_split": {k: _label_stats(v) for k, v in rows_by_split.items()},
            "by_family": {
                family: _label_stats(
                    [r for r in training_rows if r.get("signal_model_family") == family]
                )
                for family in (SCORE_EVENT_TRANSITION, NO_SCORE_DRIFT)
            },
        },
        "column_groups": {
            "identity_columns": [c for c in IDENTITY_COLUMNS if c in output_columns],
            "pre_signal_model_features": model_feature_columns,
            "pre_signal_audit_columns": audit_columns,
            "score_confirmation_columns": [
                c for c in SCORE_CONFIRMATION_COLUMNS if c in output_columns
            ],
            "label_columns": [c for c in LABEL_COLUMNS if c in output_columns],
            "all_output_columns": list(output_columns),
        },
        "family_outputs": dict(family_output_paths),
        "leakage_policy": {
            "row_unit": "One row per calibration-opportunity candidate, after the configured score-event repeat policy.",
            "score_event_repeat_policy": "Score-event rows are grouped by (mode, game, line, inning, half, outs, runners, score, reason) so repeated polling of the same baseball state does not inflate model evidence.",
            "selected_inferred_state_backfill": "Missing selected inferred-state summary fields are backfilled from same-day raw candidate rows when available, then derived from the selected +1/+2/+3 inference-panel row.",
            "stage1_support_backfill": "Missing Stage-1 support/trust fields are reconstructed from logged cache state-cell keys when available. This does not change fair value or labels.",
            "feature_timing": "pre_signal_model_features are decision-time fields only. score_confirmation_columns and label_columns are outcomes/diagnostics and must not be model inputs.",
            "selection_bias": "Includes trade, skip_with_features, and shadow_no_score_drift opportunities so training is not limited to live placed orders.",
            "family_separation": "Family-specific JSONL/CSV outputs are written alongside the master table. Score-event and no-score drift calibration/promotions should use their family file, not pooled rows.",
            "split_policy": "Contiguous non-overlapping temporal split by session_date.",
        },
        "warnings_count": len(warnings),
        "warnings": list(warnings)[:300],
        "status": "ok",
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)


def _load_all_sources(
    args: argparse.Namespace,
    warnings: List[str],
) -> Tuple[List[Dict[str, Any]], Dict[str, Dict[str, Any]], Dict[int, Dict[str, int]]]:
    rows: List[Dict[str, Any]] = []
    confirmations: Dict[str, Dict[str, Any]] = {}
    outcome_final_scores: Dict[int, Dict[str, int]] = {}
    min_date = args.min_date or None
    max_date = args.max_date or None
    if args.mode in ("live", "both"):
        rows.extend(
            _load_opportunity_rows(
                mode="live",
                root=args.live_root,
                min_date=min_date,
                max_date=max_date,
                warnings=warnings,
            )
        )
        confirmations.update(
            _load_score_confirmations(
                mode="live",
                root=args.live_root,
                min_date=min_date,
                max_date=max_date,
                warnings=warnings,
            )
        )
        outcome_final_scores.update(
            _load_outcome_final_scores(
                mode="live",
                root=args.live_root,
                min_date=min_date,
                max_date=max_date,
                warnings=warnings,
            )
        )
    if args.mode in ("paper", "both"):
        rows.extend(
            _load_opportunity_rows(
                mode="paper",
                root=args.paper_root,
                min_date=min_date,
                max_date=max_date,
                warnings=warnings,
            )
        )
        confirmations.update(
            _load_score_confirmations(
                mode="paper",
                root=args.paper_root,
                min_date=min_date,
                max_date=max_date,
                warnings=warnings,
            )
        )
        outcome_final_scores.update(
            _load_outcome_final_scores(
                mode="paper",
                root=args.paper_root,
                min_date=min_date,
                max_date=max_date,
                warnings=warnings,
            )
        )
    return rows, confirmations, outcome_final_scores


def main(argv: Optional[List[str]] = None) -> None:
    args = parse_args(argv)
    if args.min_date:
        datetime.strptime(args.min_date, "%Y-%m-%d")
    if args.max_date:
        datetime.strptime(args.max_date, "%Y-%m-%d")
    if args.min_date and args.max_date and args.min_date > args.max_date:
        raise SystemExit("--min-date must be <= --max-date")
    if args.val_frac < 0 or args.test_frac < 0 or args.val_frac + args.test_frac >= 1.0:
        raise SystemExit("--val-frac and --test-frac must be >= 0 and sum to < 1.0")

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s %(message)s",
    )

    warnings: List[str] = []
    source_rows, confirmations, outcome_final_scores = _load_all_sources(args, warnings)
    selected_inferred_state_backfill_stats = backfill_selected_inferred_state_fields(
        source_rows,
        raw_backfill_by_candidate_id={},
    )
    raw_backfill_by_candidate_id: Dict[str, Dict[str, Any]] = {}
    if not bool(args.disable_raw_candidate_backfill):
        needed_ids, needed_dates = _raw_candidate_backfill_needs(source_rows)
        if args.mode in ("live", "both"):
            raw_backfill_by_candidate_id.update(
                _load_raw_candidate_backfill(
                    mode="live",
                    root=args.live_root,
                    needed_candidate_ids=needed_ids,
                    needed_dates=needed_dates,
                    warnings=warnings,
                )
            )
        if args.mode in ("paper", "both"):
            raw_backfill_by_candidate_id.update(
                _load_raw_candidate_backfill(
                    mode="paper",
                    root=args.paper_root,
                    needed_candidate_ids=needed_ids,
                    needed_dates=needed_dates,
                    warnings=warnings,
                )
            )
    if raw_backfill_by_candidate_id:
        selected_inferred_state_backfill_stats = _merge_backfill_stats(
            selected_inferred_state_backfill_stats,
            backfill_selected_inferred_state_fields(
                source_rows,
                raw_backfill_by_candidate_id=raw_backfill_by_candidate_id,
            ),
        )
    stage1_support_backfill_stats = {
        "rows_seen": len(source_rows),
        "cells_loaded": 0,
        "rows_with_inferred_support_backfill": 0,
        "fields_inferred_support_backfilled": 0,
        "rows_with_current_support_backfill": 0,
        "fields_current_support_backfilled": 0,
    }
    if not bool(args.disable_stage1_support_backfill):
        stage1_cells = _load_stage1_cache_cells(args.stage1_cache_path, warnings)
        stage1_support_backfill_stats = backfill_stage1_support_fields(
            source_rows,
            cells=stage1_cells,
        )
    split_map, date_rank, split_dates = _build_date_split_map(
        [str(r.get("session_date") or "") for r in source_rows],
        val_frac=args.val_frac,
        test_frac=args.test_frac,
    )
    scraped_final_scores = _load_final_scores(args.games_root, warnings)
    final_scores = dict(scraped_final_scores)
    final_scores.update(outcome_final_scores)
    training_rows = build_training_rows(
        source_rows,
        confirmations_by_candidate_id=confirmations,
        final_scores_by_game_pk=final_scores,
        split_map=split_map,
        date_rank=date_rank,
        drop_unlabeled_final=bool(args.drop_unlabeled_final),
    )
    training_rows, repeat_policy_stats = apply_score_event_repeat_policy(
        training_rows,
        policy=args.score_event_repeat_policy,
    )
    output_columns = _infer_output_columns(training_rows)

    if args.strict:
        if not training_rows:
            raise SystemExit("Strict mode failed: no calibration-opportunity rows found.")
        if not any(r.get("label_final_available") for r in training_rows):
            raise SystemExit("Strict mode failed: no final-score labels joined.")
        score_event_rows = [
            r for r in training_rows if r.get("signal_model_family") == SCORE_EVENT_TRANSITION
        ]
        if score_event_rows and not any(
            r.get("label_score_confirmation_available") for r in score_event_rows
        ):
            raise SystemExit("Strict mode failed: no score confirmations joined for score-event rows.")

    args.output_root.mkdir(parents=True, exist_ok=True)
    jsonl_path = args.output_root / f"{args.output_stem}.jsonl"
    csv_path = args.output_root / f"{args.output_stem}.csv"
    manifest_path = args.output_root / f"{args.output_stem}_manifest.json"

    _write_jsonl(jsonl_path, training_rows, output_columns)
    _write_csv(csv_path, training_rows, output_columns)

    family_output_paths: Dict[str, Dict[str, Any]] = {}
    by_family_dir = args.output_root / "by_family"
    by_family_dir.mkdir(parents=True, exist_ok=True)
    for family in sorted({str(r.get("signal_model_family") or "unknown") for r in training_rows}):
        family_rows = [r for r in training_rows if str(r.get("signal_model_family") or "unknown") == family]
        family_stem = f"{args.output_stem}_{family}"
        family_jsonl = by_family_dir / f"{family_stem}.jsonl"
        family_csv = by_family_dir / f"{family_stem}.csv"
        _write_jsonl(family_jsonl, family_rows, output_columns)
        _write_csv(family_csv, family_rows, output_columns)
        family_output_paths[family] = {
            "jsonl": str(family_jsonl),
            "csv": str(family_csv),
            "rows": len(family_rows),
        }

    write_manifest(
        path=manifest_path,
        args=args,
        source_rows=source_rows,
        confirmations_by_candidate_id=confirmations,
        outcome_final_scores_by_game_pk=outcome_final_scores,
        scraped_final_scores_by_game_pk=scraped_final_scores,
        training_rows=training_rows,
        split_dates=split_dates,
        output_columns=output_columns,
        family_output_paths=family_output_paths,
        repeat_policy_stats=repeat_policy_stats,
        selected_inferred_state_backfill_stats=selected_inferred_state_backfill_stats,
        stage1_support_backfill_stats=stage1_support_backfill_stats,
        warnings=warnings,
    )

    LOGGER.info("Wrote %s", jsonl_path)
    LOGGER.info("Wrote %s", csv_path)
    LOGGER.info("Wrote %s", manifest_path)
    LOGGER.info(
        "Rows: source=%d output=%d final_labels=%d score_confirmations=%d "
        "selected_inferred_rows=%d current_support_backfill_rows=%d "
        "score_event_repeat_policy=%s collapsed=%d",
        len(source_rows),
        len(training_rows),
        sum(1 for r in training_rows if r.get("label_final_available")),
        sum(1 for r in training_rows if r.get("label_score_confirmation_available")),
        int(selected_inferred_state_backfill_stats.get("rows_with_selected_summary_after") or 0),
        int(stage1_support_backfill_stats.get("rows_with_current_support_backfill") or 0),
        args.score_event_repeat_policy,
        int(repeat_policy_stats.get("score_event_collapsed_rows") or 0),
    )


if __name__ == "__main__":
    main()
