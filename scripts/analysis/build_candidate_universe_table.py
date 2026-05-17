#!/usr/bin/env python3
"""
Build a decision-level candidate-universe table (trade + no-trade rows).

Inputs:
  data/live_trading/candidate_universe/*.jsonl
  data/paper_trading/candidate_universe/*.jsonl
  data/games/regular/**/**.json  (final score labels)

Outputs:
  data/analysis_output/candidate_universe/
    candidates_master.jsonl
    candidates_master.csv
    build_manifest.json
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple


PROJECT_DIR = Path(__file__).resolve().parents[2]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from scripts.trading.model_families import infer_signal_model_family
from scripts.trading.remaining_opportunity import compute_remaining_opportunity_fields
from scripts.trading.shadow_diagnostic_features import compute_shadow_diagnostic_fields
from scripts.trading.stage1_support import STAGE1_SUPPORT_SUFFIXES, stage1_support_field_names
from scripts.trading.weather_client import WEATHER_FEATURE_FIELD_KEYS

DEFAULT_OUTPUT_ROOT = PROJECT_DIR / "data" / "analysis_output" / "candidate_universe"
LIVE_CANDIDATES_ROOT = PROJECT_DIR / "data" / "live_trading" / "candidate_universe"
PAPER_CANDIDATES_ROOT = PROJECT_DIR / "data" / "paper_trading" / "candidate_universe"
GAMES_ROOT = PROJECT_DIR / "data" / "games" / "regular"

FILE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})_candidates\.jsonl$")

LOGGER = logging.getLogger("build_candidate_universe_table")

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


OUTPUT_COLUMNS = [
    "schema_version",
    "mode",
    "session_date",
    "candidate_id",
    "ts",
    "recorded_at",
    "game_pk",
    "away_abbrev",
    "home_abbrev",
    "line",
    "side",
    "signal_model_family",
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
    *WEATHER_FEATURE_FIELD_KEYS,
    *MARKET_COMPLEMENT_COLUMNS,
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
    "current_state_value_lookup_error",
    "runs_needed",
    "lead_abs",
    "decision_ask",
    "best_bid",
    "spread",
    "min_inning_effective",
    "high_line_cutoff",
    "inactive_inning_state",
    "min_entry_ask_effective",
    "crossed_book_bid",
    "crossed_book_ask",
    "max_spread_effective",
    "ask_jump",
    "jump_threshold_effective",
    "lookback_ticks",
    "baseline_ask",
    "pending_jump_ask",
    "pending_ticks_remaining",
    "confirmation_ticks",
    "confirmation_status",
    "fair_value",
    "base_fair_value",
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
    "stage2_run_env_delta",
    "team_offense_delta",
    "edge",
    "min_edge_base",
    "min_edge_ask_boost",
    "min_edge_effective",
    "inferred_runs",
    "fair_value_raw",
    "fair_value_calibrated",
    "fair_value_calibration_delta",
    "fair_value_calibration_method",
    "fair_value_calibration_mode",
    "fair_value_calibration_family",
    "fair_value_calibration_applied",
    "inference_used_fallback",
    "inference_state_fallback_level",
    "inference_state_fallback_label",
    "inference_state_cell_key",
    "inference_line_requested_key",
    "inference_line_fallback_mode",
    "inference_line_source_key",
    "inference_line_source_key_low",
    "inference_line_source_key_high",
    *INFERENCE_PANEL_COLUMNS,
    "posted_limit",
    "decision",
    "decision_reason",
    "bet_id",
    "shadow_relaxed_evaluated",
    "shadow_relaxed_would_pass",
    "shadow_relaxed_reason",
    "shadow_relaxed_value",
    "shadow_relaxed_threshold",
    "shadow_relaxed_comparator",
    "shadow_relaxed_secondary_evaluated",
    "shadow_relaxed_secondary_would_pass",
    "shadow_relaxed_secondary_reason",
    "shadow_relaxed_secondary_value",
    "shadow_relaxed_secondary_threshold",
    "shadow_relaxed_secondary_comparator",
    "conditional_relax_gate",
    "conditional_relax_mode",
    "conditional_relax_arm",
    "conditional_relax_would_pass",
    "conditional_relax_applied",
    "shadow_fv_current_state",
    "shadow_fv_after_inferred_score",
    "shadow_fv_inferred_lift",
    "shadow_no_event_edge",
    "shadow_after_event_edge",
    "shadow_p_score_event_proxy",
    "shadow_phantom_risk_score",
    "shadow_phantom_risk_band",
    "shadow_transition_model",
    "shadow_transition_inferred_runs",
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
    "state_value_strategy",
    "score_segment_key",
    "score_segment_age_secs",
    "score_segment_ticks",
    "score_segment_high_ask",
    "score_segment_drawdown",
    "shadow_no_score_drift_min_age_secs",
    "shadow_no_score_drift_min_ticks",
    "shadow_no_score_drift_min_drawdown",
    "shadow_no_score_drift_min_ask",
    "shadow_no_score_drift_max_ask",
    "shadow_no_score_drift_max_spread",
    "shadow_no_score_drift_min_po_edge",
    "shadow_no_score_drift_min_emp_edge",
    "shadow_no_score_drift_trigger",
    "source_path",
    "target_trade",
    "label_available",
    "final_total",
    "target_counterfactual_win",
]


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Build decision-level candidate-universe table.")
    p.add_argument(
        "--mode",
        choices=["live", "paper", "both"],
        default="both",
        help="Candidate source mode to include (default: both).",
    )
    p.add_argument("--min-date", type=str, default="", help="Inclusive lower date bound (YYYY-MM-DD).")
    p.add_argument("--max-date", type=str, default="", help="Inclusive upper date bound (YYYY-MM-DD).")
    p.add_argument(
        "--output-root",
        type=Path,
        default=DEFAULT_OUTPUT_ROOT,
        help=f"Output directory (default: {DEFAULT_OUTPUT_ROOT}).",
    )
    p.add_argument("--strict", action="store_true", help="Fail if required checks fail.")
    p.add_argument("--verbose", action="store_true", help="Verbose logging.")
    return p.parse_args(argv)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _safe_int(v: Any) -> Optional[int]:
    try:
        if v is None or v == "":
            return None
        return int(v)
    except Exception:
        return None


def _safe_float(v: Any) -> Optional[float]:
    try:
        if v is None or v == "":
            return None
        return float(v)
    except Exception:
        return None


def _date_in_range(date_str: str, min_date: Optional[str], max_date: Optional[str]) -> bool:
    if not date_str:
        return True
    if min_date and date_str < min_date:
        return False
    if max_date and date_str > max_date:
        return False
    return True


def _iter_candidate_files(root: Path) -> Iterable[Tuple[str, Path]]:
    if not root.exists():
        return
    for p in sorted(root.glob("*_candidates.jsonl")):
        m = FILE_RE.match(p.name)
        if not m:
            continue
        yield m.group(1), p


def _load_mode_rows(
    mode: str,
    root: Path,
    min_date: Optional[str],
    max_date: Optional[str],
    warnings: List[str],
) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for session_date, path in _iter_candidate_files(root):
        if not _date_in_range(session_date, min_date=min_date, max_date=max_date):
            continue
        with open(path, encoding="utf-8") as f:
            for i, raw in enumerate(f, start=1):
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    row = json.loads(raw)
                except Exception as exc:
                    warnings.append(f"[{mode}] malformed JSON {path}:{i}: {exc}")
                    continue
                if not isinstance(row, dict):
                    warnings.append(f"[{mode}] non-dict row {path}:{i}")
                    continue
                out = dict(row)
                out["mode"] = str(out.get("mode") or mode)
                out["session_date"] = str(out.get("session_date") or session_date)
                out["source_path"] = str(path)
                rows.append(out)
    return rows


def _load_final_totals(games_root: Path, warnings: List[str]) -> Dict[int, int]:
    finals: Dict[int, int] = {}
    if not games_root.exists():
        warnings.append(f"games root does not exist: {games_root}")
        return finals
    for year_dir in sorted(p for p in games_root.iterdir() if p.is_dir()):
        for date_dir in sorted(p for p in year_dir.iterdir() if p.is_dir()):
            for game_path in sorted(date_dir.glob("*.json")):
                try:
                    with open(game_path, encoding="utf-8") as f:
                        game = json.load(f)
                except Exception as exc:
                    warnings.append(f"failed to parse game file {game_path}: {exc}")
                    continue
                game_pk = _safe_int(game.get("gamePk"))
                if game_pk is None:
                    continue
                linescore = game.get("liveData", {}).get("linescore", {})
                teams = linescore.get("teams", {})
                away = _safe_int(teams.get("away", {}).get("runs"))
                home = _safe_int(teams.get("home", {}).get("runs"))
                if away is None or home is None:
                    continue
                finals[game_pk] = away + home
    return finals


def _enrich_labels(rows: List[Dict[str, Any]], final_totals: Dict[int, int]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for row in rows:
        r = {k: None for k in OUTPUT_COLUMNS}
        r.update(row)
        r.update(
            compute_remaining_opportunity_fields(
                away_score=r.get("away_score_before"),
                home_score=r.get("home_score_before"),
                inning=r.get("inning"),
                inning_state=r.get("inning_state"),
            )
        )
        for field, value in compute_shadow_diagnostic_fields(r).items():
            if r.get(field) in (None, ""):
                r[field] = value
        r["signal_model_family"] = infer_signal_model_family(r)
        decision = str(row.get("decision") or "").lower()
        if decision == "trade":
            target_trade = 1
        elif decision in ("skip", "skip_with_features", "shadow_no_score_drift"):
            target_trade = 0
        else:
            target_trade = None

        game_pk = _safe_int(row.get("game_pk"))
        line = _safe_float(row.get("line"))
        final_total = final_totals.get(game_pk) if game_pk is not None else None
        label_available = bool(final_total is not None and line is not None)
        target_counterfactual_win = None
        if label_available and final_total is not None and line is not None:
            target_counterfactual_win = 1 if final_total > line else 0

        r["target_trade"] = target_trade
        r["label_available"] = label_available
        r["final_total"] = final_total
        r["target_counterfactual_win"] = target_counterfactual_win
        out.append(r)

    out.sort(
        key=lambda x: (
            str(x.get("session_date") or ""),
            str(x.get("mode") or ""),
            str(x.get("candidate_id") or ""),
            str(x.get("ts") or ""),
        )
    )
    return out


def _write_jsonl(path: Path, rows: Iterable[Dict[str, Any]]) -> None:
    with open(path, "w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row) + "\n")


def _write_csv(path: Path, rows: List[Dict[str, Any]], columns: List[str]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def _rows_by_mode(rows: List[Dict[str, Any]]) -> Dict[str, int]:
    counts: Dict[str, int] = defaultdict(int)
    for row in rows:
        counts[str(row.get("mode") or "unknown")] += 1
    return dict(counts)


def _decision_counts(rows: List[Dict[str, Any]]) -> Dict[str, int]:
    counts: Dict[str, int] = defaultdict(int)
    for row in rows:
        counts[str(row.get("decision") or "unknown")] += 1
    return dict(counts)


def main(argv: Optional[List[str]] = None) -> None:
    args = parse_args(argv)
    if args.min_date:
        datetime.strptime(args.min_date, "%Y-%m-%d")
    if args.max_date:
        datetime.strptime(args.max_date, "%Y-%m-%d")
    if args.min_date and args.max_date and args.min_date > args.max_date:
        raise SystemExit("--min-date must be <= --max-date")

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s %(message)s",
    )

    warnings: List[str] = []
    source_rows: List[Dict[str, Any]] = []
    if args.mode in ("live", "both"):
        source_rows.extend(
            _load_mode_rows(
                mode="live",
                root=LIVE_CANDIDATES_ROOT,
                min_date=args.min_date or None,
                max_date=args.max_date or None,
                warnings=warnings,
            )
        )
    if args.mode in ("paper", "both"):
        source_rows.extend(
            _load_mode_rows(
                mode="paper",
                root=PAPER_CANDIDATES_ROOT,
                min_date=args.min_date or None,
                max_date=args.max_date or None,
                warnings=warnings,
            )
        )

    final_totals = _load_final_totals(GAMES_ROOT, warnings=warnings)
    rows = _enrich_labels(source_rows, final_totals=final_totals)

    if args.strict and not rows:
        raise SystemExit("Strict mode failed: no candidate rows found.")

    args.output_root.mkdir(parents=True, exist_ok=True)
    out_jsonl = args.output_root / "candidates_master.jsonl"
    out_csv = args.output_root / "candidates_master.csv"
    manifest_path = args.output_root / "build_manifest.json"

    _write_jsonl(out_jsonl, rows)
    _write_csv(out_csv, rows, OUTPUT_COLUMNS)

    manifest = {
        "generated_at_utc": _now_iso(),
        "config": {
            "mode": args.mode,
            "min_date": args.min_date or None,
            "max_date": args.max_date or None,
            "output_root": str(args.output_root),
            "strict": bool(args.strict),
        },
        "counts": {
            "source_rows": len(source_rows),
            "output_rows": len(rows),
            "rows_by_mode": _rows_by_mode(rows),
            "decision_counts": _decision_counts(rows),
            "label_available_rows": sum(1 for r in rows if r.get("label_available")),
        },
        "paths": {
            "live_candidates_root": str(LIVE_CANDIDATES_ROOT),
            "paper_candidates_root": str(PAPER_CANDIDATES_ROOT),
            "games_root": str(GAMES_ROOT),
            "output_jsonl": str(out_jsonl),
            "output_csv": str(out_csv),
        },
        "warnings_count": len(warnings),
        "warnings": warnings[:300],
        "status": "ok",
    }
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)

    LOGGER.info("Wrote %s", out_jsonl)
    LOGGER.info("Wrote %s", out_csv)
    LOGGER.info("Wrote %s", manifest_path)
    LOGGER.info(
        "Rows: source=%d output=%d labels=%d",
        len(source_rows),
        len(rows),
        sum(1 for r in rows if r.get("label_available")),
    )


if __name__ == "__main__":
    main()
