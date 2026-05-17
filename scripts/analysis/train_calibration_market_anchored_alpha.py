#!/usr/bin/env python3
"""
Train market-anchored alpha models from calibration opportunities.

This is the opportunity-table version of market anchoring:

    logit(p_over_win) = logit(market_price) + alpha(features)

The market remains the prior. The model only learns residual disagreement.
Families are trained separately by default because score-event transition and
no-score drift have different latency, selection, and label mechanics.

Inputs:
  data/analysis_output/calibration_opportunity_training/
    calibration_opportunity_training_table.jsonl

Outputs:
  data/analysis_output/calibration_market_anchored_alpha/
    calibration_market_anchored_alpha_report.json
    calibration_market_anchored_alpha_predictions.jsonl
    by_family/<family>_market_anchored_alpha_model.json
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple


PROJECT_DIR = Path(__file__).resolve().parents[2]
ANALYSIS_DIR = PROJECT_DIR / "scripts" / "analysis"
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))
if str(ANALYSIS_DIR) not in sys.path:
    sys.path.insert(0, str(ANALYSIS_DIR))

import train_market_anchored_alpha as maa  # noqa: E402
from scripts.trading.scoring_path_features import SCORING_PATH_MODEL_FIELD_KEYS  # noqa: E402


LOGGER = logging.getLogger("train_calibration_market_anchored_alpha")

DEFAULT_TABLE_PATH = (
    PROJECT_DIR
    / "data"
    / "analysis_output"
    / "calibration_opportunity_training"
    / "calibration_opportunity_training_table.jsonl"
)
DEFAULT_OUTPUT_ROOT = PROJECT_DIR / "data" / "analysis_output" / "calibration_market_anchored_alpha"

FAMILIES = ("score_event_transition", "no_score_drift")

CALIBRATION_ALPHA_FEATURE_COLUMNS = [
    "signal_model_family",
    "state_value_strategy",
    "decision",
    "decision_reason",
    "line",
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
    *SCORING_PATH_MODEL_FIELD_KEYS,
    "market_price",
    "market_mid",
    "market_spread",
    "opposite_market_price",
    "over_under_ask_sum",
    "over_under_bid_sum",
    "over_under_mid_sum",
    "over_mid_no_vig",
    "under_mid_no_vig",
    "decision_market_mid_no_vig",
    "raw_model_probability",
    "raw_model_edge_to_ask",
    "raw_model_edge_to_mid_no_vig",
    "model_market_logit_residual",
    "model_market_mid_no_vig_logit_residual",
    "fair_value_raw",
    "fair_value_calibrated",
    "base_fair_value",
    "inferred_state_base_poisson",
    "inferred_state_base_empirical",
    "inferred_state_poisson_minus_empirical",
    "inferred_state_empirical_edge",
    "inferred_state_effective_n",
    "inferred_state_fallback_level",
    "inferred_state_fallback_label",
    "inferred_state_line_fallback_mode",
    "inferred_state_empirical_line_fallback_mode",
    "stage2_run_env_delta",
    "stage2_weather_source",
    "stage2_weather_model_usable",
    "team_offense_delta",
    "current_state_value_base_poisson",
    "current_state_value_base_empirical",
    "current_state_value_edge",
    "current_state_value_empirical_edge",
    "current_state_value_fv_raw",
    "shadow_fv_inferred_lift",
    "shadow_no_event_edge",
    "shadow_after_event_edge",
    "shadow_p_score_event_proxy",
    "shadow_phantom_risk_score",
    "shadow_phantom_risk_band",
    "shadow_low_ask_high_edge",
    "shadow_runs_needed_exact_3p5",
    "shadow_current_state_edge_bucket",
    "shadow_phantom_risk_bucket",
    "shadow_current_phantom_combo_bucket",
    "shadow_inning_bucket",
    "shadow_inning_runs_needed_bucket",
    "shadow_bottom9_home_lead_context",
    "score_segment_age_secs",
    "score_segment_drawdown",
    "shadow_no_score_drift_trigger",
    "weather_model_usable",
    "weather_source_status",
    "stadium_weather_exposure",
    "stadium_weather_sensitivity",
    "weather_v2_air_temp_f",
    "weather_v2_wind_speed_mph",
    "weather_v2_wind_out_to_cf_mph",
    "weather_v2_relative_humidity_pct",
    "weather_v2_air_density_kg_m3",
    "inference_panel_selected_runs",
    "inference_run1_base_poisson",
    "inference_run1_base_empirical",
    "inference_run1_distance_to_ask",
    "inference_run1_poisson_minus_empirical",
    "inference_run2_base_poisson",
    "inference_run2_base_empirical",
    "inference_run2_distance_to_ask",
    "inference_run2_poisson_minus_empirical",
    "inference_run3_base_poisson",
    "inference_run3_base_empirical",
    "inference_run3_distance_to_ask",
    "inference_run3_poisson_minus_empirical",
]


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Train market-anchored alpha models from calibration opportunities."
    )
    p.add_argument("--table-path", type=Path, default=DEFAULT_TABLE_PATH)
    p.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    p.add_argument("--mode", choices=["live", "paper", "both"], default="live")
    p.add_argument("--family", choices=["all", *FAMILIES], default="all")
    p.add_argument("--anchor-price", choices=["ask", "mid_no_vig"], default="ask")
    p.add_argument("--min-date", type=str, default="")
    p.add_argument("--max-date", type=str, default="")
    p.add_argument("--val-frac", type=float, default=0.20)
    p.add_argument("--test-frac", type=float, default=0.20)
    p.add_argument("--min-train-rows", type=int, default=40)
    p.add_argument("--max-iter", type=int, default=200)
    p.add_argument(
        "--artifact-purpose",
        choices=["evaluation", "runtime-refit"],
        default="evaluation",
        help=(
            "evaluation writes the split-trained model used for metrics. "
            "runtime-refit keeps train/validation/test evaluation unchanged, "
            "then refits exported family model artifacts on all eligible "
            "labeled rows using the selected hyperparameters."
        ),
    )
    p.add_argument("--strict", action="store_true")
    p.add_argument("--verbose", action="store_true")
    return p.parse_args(argv)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _read_jsonl(path: Path) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    with path.open(encoding="utf-8") as f:
        for line_no, raw in enumerate(f, 1):
            text = raw.strip()
            if not text:
                continue
            try:
                obj = json.loads(text)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{line_no}: invalid JSONL row: {exc}") from exc
            if isinstance(obj, dict):
                rows.append(obj)
    return rows


def _date_value(row: Mapping[str, Any]) -> str:
    value = str(row.get("session_date") or "").strip()
    if len(value) >= 10:
        return value[:10]
    ts = str(row.get("ts") or row.get("recorded_at") or "").strip()
    return ts[:10] if len(ts) >= 10 else ""


def _date_in_range(date_str: str, min_date: str, max_date: str) -> bool:
    if min_date and date_str and date_str < min_date:
        return False
    if max_date and date_str and date_str > max_date:
        return False
    return True


def _family(row: Mapping[str, Any]) -> str:
    return str(row.get("signal_model_family") or row.get("state_value_strategy") or "unknown")


def _pick_market_price(row: Mapping[str, Any], anchor_price: str) -> Optional[float]:
    if anchor_price == "mid_no_vig":
        price = maa._safe_float(row.get("decision_market_mid_no_vig"))
        if price is not None and 0.0 < price < 1.0:
            return price
    return maa._safe_float(row.get("decision_ask"))


def _calibration_alpha_row(
    row: Mapping[str, Any],
    *,
    split: str,
    anchor_price: str,
) -> Optional[Dict[str, Any]]:
    target_win = maa._label_int(row.get("target_over_win"))
    market_price = _pick_market_price(row, anchor_price)
    if target_win is None or market_price is None or not 0.0 < market_price < 1.0:
        return None
    raw_model_probability = maa._safe_float(row.get("fair_value"))
    if raw_model_probability is None:
        raw_model_probability = market_price
    raw_model_mid_edge = None
    mid_no_vig = maa._safe_float(row.get("decision_market_mid_no_vig"))
    if mid_no_vig is not None:
        raw_model_mid_edge = raw_model_probability - mid_no_vig
    source_id = str(row.get("candidate_id") or row.get("bet_id") or row.get("outcome_join_key") or "")
    out: Dict[str, Any] = {
        "side_row_id": f"{source_id}|over|{anchor_price}",
        "source_row_id": source_id,
        "session_date": _date_value(row),
        "split": split,
        "side": "over",
        "target_win": target_win,
        "market_price": market_price,
        "market_logit": maa._logit(market_price),
        "market_mid": maa._safe_float(row.get("decision_mid") or row.get("over_mid")),
        "market_spread": maa._safe_float(row.get("spread") or row.get("over_spread")),
        "opposite_market_price": maa._safe_float(row.get("under_best_ask")),
        "raw_model_probability": raw_model_probability,
        "raw_model_edge_to_ask": raw_model_probability - market_price,
        "raw_model_edge_to_mid_no_vig": raw_model_mid_edge,
        "model_market_logit_residual": row.get("model_market_logit_residual"),
    }
    for key in CALIBRATION_ALPHA_FEATURE_COLUMNS:
        if key in out:
            continue
        out[key] = row.get(key)
    for passthrough in (
        "candidate_id",
        "game_pk",
        "away_abbrev",
        "home_abbrev",
        "line",
        "decision_reason",
        "signal_model_family",
        "state_value_strategy",
        "target_taker_profit_units",
        "target_limit_profit_units",
    ):
        out[passthrough] = row.get(passthrough)
    return out


def build_alpha_rows(
    rows: Sequence[Dict[str, Any]],
    *,
    mode: str,
    family: str,
    anchor_price: str,
    min_date: str,
    max_date: str,
    val_frac: float,
    test_frac: float,
) -> Tuple[List[Dict[str, Any]], Dict[str, List[str]]]:
    filtered: List[Dict[str, Any]] = []
    for row in rows:
        row_mode = str(row.get("mode") or "").strip()
        if mode != "both" and row_mode and row_mode != mode:
            continue
        if family != "all" and _family(row) != family:
            continue
        date = _date_value(row)
        if not _date_in_range(date, min_date, max_date):
            continue
        filtered.append(row)
    split_map, split_dates = maa.build_split_map(
        [_date_value(r) for r in filtered],
        val_frac=val_frac,
        test_frac=test_frac,
    )
    out: List[Dict[str, Any]] = []
    for row in filtered:
        split = split_map.get(_date_value(row), "train")
        alpha_row = _calibration_alpha_row(row, split=split, anchor_price=anchor_price)
        if alpha_row is not None:
            out.append(alpha_row)
    out.sort(key=lambda r: (str(r.get("session_date")), str(r.get("side_row_id"))))
    return out, split_dates


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    with path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)


def _write_jsonl(path: Path, rows: Iterable[Mapping[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(dict(row)) + "\n")


def _write_markdown(path: Path, report: Mapping[str, Any]) -> None:
    lines = [
        "# Calibration Market-Anchored Alpha Report",
        "",
        f"Generated: {report.get('generated_at_utc')}",
        "",
        f"Anchor price: `{(report.get('config') or {}).get('anchor_price')}`",
        "",
        "| family | status | rows | train | validation | test | market test brier | raw test brier | alpha test brier |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for family, payload in (report.get("families") or {}).items():
        rows = payload.get("rows") or {}
        metrics = (((payload.get("report") or {}).get("metrics") or {}).get("test") or {})
        market = metrics.get("market") or {}
        raw = metrics.get("raw_model") or {}
        alpha = metrics.get("market_anchored_alpha") or {}
        lines.append(
            "| "
            + " | ".join(
                [
                    str(family),
                    str(payload.get("status")),
                    str(rows.get("total")),
                    str(rows.get("train")),
                    str(rows.get("validation")),
                    str(rows.get("test")),
                    "" if market.get("brier") is None else f"{market.get('brier'):.4f}",
                    "" if raw.get("brier") is None else f"{raw.get('brier'):.4f}",
                    "" if alpha.get("brier") is None else f"{alpha.get('brier'):.4f}",
                ]
            )
            + " |"
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _refit_alpha_model_payload(
    rows: List[Dict[str, Any]],
    *,
    evaluation_model_payload: Mapping[str, Any],
    feature_cols: Sequence[str],
    max_iter: int,
    strict: bool,
) -> Dict[str, Any]:
    if strict and not rows:
        raise SystemExit("Strict mode failed: no runtime-refit rows for alpha model.")
    if strict and len(set(int(r.get("target_win")) for r in rows)) < 2:
        raise SystemExit("Strict mode failed: runtime-refit alpha rows contain only one class.")
    if not rows:
        return dict(evaluation_model_payload)

    prep = maa.tbm.fit_preprocessor(rows, list(feature_cols))
    X, y, offsets = maa._dataset(rows, prep)
    if not X:
        return dict(evaluation_model_payload)

    selected = dict(evaluation_model_payload.get("search_selected") or {})
    l2 = float(selected.get("l2", 0.1))
    lr = float(selected.get("lr", 0.05))
    model = maa.fit_offset_logistic(
        X,
        y,
        offsets,
        l2=l2,
        lr=lr,
        max_iter=max_iter,
    )
    probs = maa.predict_offset(model, X, offsets)
    top_coefficients = [
        {"feature": name, "weight": weight}
        for name, weight in sorted(
            zip(prep.feature_names, model.weights),
            key=lambda pair: abs(pair[1]),
            reverse=True,
        )[:20]
    ]
    payload = dict(evaluation_model_payload)
    payload["artifact_purpose"] = "runtime-refit"
    payload["fit_scope"] = "all_eligible_labeled_rows_after_hyperparameter_selection"
    payload["selection_source"] = "train_validation_test_evaluation_split"
    payload["evaluation_model"] = {
        "model": evaluation_model_payload.get("model"),
        "preprocessor": evaluation_model_payload.get("preprocessor"),
        "weights": evaluation_model_payload.get("weights"),
        "top_coefficients": evaluation_model_payload.get("top_coefficients"),
        "metrics": evaluation_model_payload.get("metrics"),
    }
    selected.update(
        {
            "runtime_refit_rows": len(rows),
            "runtime_refit_scope": "all_eligible_labeled_rows_after_hyperparameter_selection",
        }
    )
    payload["search_selected"] = selected
    payload["model"] = {
        "bias": model.bias,
        "weights": model.weights,
        "l2": model.l2,
        "lr": model.lr,
        "iterations": model.iterations,
        "train_loss": model.train_loss,
    }
    payload["preprocessor"] = {
        "numeric_cols": prep.numeric_cols,
        "categorical_cols": prep.categorical_cols,
        "medians": prep.medians,
        "means": prep.means,
        "stds": prep.stds,
        "categories": prep.categories,
        "feature_names": prep.feature_names,
    }
    payload["weights"] = [
        {"feature": name, "weight": weight}
        for name, weight in zip(prep.feature_names, model.weights)
    ]
    payload["top_coefficients"] = top_coefficients
    metrics = dict(evaluation_model_payload.get("metrics") or {})
    metrics["runtime_refit"] = maa._subset_metrics(rows, probs)
    payload["metrics"] = metrics
    return payload


def main(argv: Optional[List[str]] = None) -> None:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s %(message)s",
    )
    if args.min_date:
        datetime.strptime(args.min_date, "%Y-%m-%d")
    if args.max_date:
        datetime.strptime(args.max_date, "%Y-%m-%d")
    if args.min_date and args.max_date and args.min_date > args.max_date:
        raise SystemExit("--min-date must be <= --max-date")
    if not args.table_path.exists():
        raise SystemExit(f"Missing table path: {args.table_path}")

    source_rows = _read_jsonl(args.table_path)
    families = FAMILIES if args.family == "all" else (args.family,)
    report: Dict[str, Any] = {
        "generated_at_utc": _now_iso(),
        "description": (
            "Family-separated market-anchored alpha models from calibration opportunities. "
            "This is research-only; market price remains the fixed prior."
        ),
        "config": {
            "table_path": str(args.table_path),
            "output_root": str(args.output_root),
            "mode": args.mode,
            "family": args.family,
            "anchor_price": args.anchor_price,
            "min_date": args.min_date or None,
            "max_date": args.max_date or None,
            "val_frac": args.val_frac,
            "test_frac": args.test_frac,
            "min_train_rows": args.min_train_rows,
            "max_iter": args.max_iter,
            "artifact_purpose": args.artifact_purpose,
        },
        "source_rows": len(source_rows),
        "families": {},
        "warnings": [
            "This model is trained on opportunities, not independent games.",
            "Do not promote until walk-forward stability beats market ask/no-vig baselines by family.",
        ],
    }
    all_predictions: List[Dict[str, Any]] = []
    args.output_root.mkdir(parents=True, exist_ok=True)
    by_family_dir = args.output_root / "by_family"
    by_family_dir.mkdir(parents=True, exist_ok=True)

    for family in families:
        alpha_rows, split_dates = build_alpha_rows(
            source_rows,
            mode=args.mode,
            family=family,
            anchor_price=args.anchor_price,
            min_date=args.min_date,
            max_date=args.max_date,
            val_frac=args.val_frac,
            test_frac=args.test_frac,
        )
        status = "ok"
        family_payload: Dict[str, Any] = {
            "status": status,
            "rows": dict(Counter(str(r.get("split") or "train") for r in alpha_rows)),
            "split_dates": split_dates,
        }
        family_payload["rows"]["total"] = len(alpha_rows)
        try:
            family_report, model_payload, pred_rows = maa.train_alpha_model(
                alpha_rows,
                feature_cols=CALIBRATION_ALPHA_FEATURE_COLUMNS,
                min_train_rows=args.min_train_rows,
                max_iter=args.max_iter,
                strict=args.strict,
            )
        except SystemExit as exc:
            if args.strict:
                raise
            status = "skipped"
            family_payload["status"] = status
            family_payload["skip_reason"] = str(exc)
            report["families"][family] = family_payload
            continue
        model_payload["source"] = "calibration_opportunity_training"
        model_payload["family"] = family
        model_payload["anchor_price"] = args.anchor_price
        model_payload["artifact_purpose"] = args.artifact_purpose
        model_payload["fit_scope"] = "train_split"
        model_payload["selection_source"] = "train_validation_test_evaluation_split"
        model_payload["split_dates"] = split_dates
        family_report["source"] = "calibration_opportunity_training"
        family_report["family"] = family
        family_report["anchor_price"] = args.anchor_price
        family_report["artifact_purpose"] = args.artifact_purpose
        family_report["split_dates"] = split_dates
        for pred in pred_rows:
            pred["family"] = family
        all_predictions.extend(pred_rows)
        artifact_model_payload = model_payload
        if args.artifact_purpose == "runtime-refit":
            artifact_model_payload = _refit_alpha_model_payload(
                alpha_rows,
                evaluation_model_payload=model_payload,
                feature_cols=CALIBRATION_ALPHA_FEATURE_COLUMNS,
                max_iter=args.max_iter,
                strict=args.strict,
            )
            artifact_model_payload["source"] = "calibration_opportunity_training"
            artifact_model_payload["family"] = family
            artifact_model_payload["anchor_price"] = args.anchor_price
            artifact_model_payload["split_dates"] = split_dates

        model_path = by_family_dir / f"{family}_market_anchored_alpha_model.json"
        family_report_path = by_family_dir / f"{family}_market_anchored_alpha_report.json"
        _write_json(model_path, artifact_model_payload)
        _write_json(family_report_path, family_report)
        family_payload.update(
            {
                "status": "ok",
                "model_path": str(model_path),
                "family_report_path": str(family_report_path),
                "report": family_report,
            }
        )
        report["families"][family] = family_payload

    pred_path = args.output_root / "calibration_market_anchored_alpha_predictions.jsonl"
    report_path = args.output_root / "calibration_market_anchored_alpha_report.json"
    md_path = args.output_root / "calibration_market_anchored_alpha_report.md"
    _write_jsonl(pred_path, all_predictions)
    _write_json(report_path, report)
    _write_markdown(md_path, report)
    LOGGER.info("Wrote %s", report_path)
    LOGGER.info("Wrote %s", md_path)
    LOGGER.info("Wrote %s", pred_path)


if __name__ == "__main__":
    main()
