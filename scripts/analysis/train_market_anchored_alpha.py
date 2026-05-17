#!/usr/bin/env python3
"""
Train a market-anchored side-selection alpha model.

The model does not learn a free-floating win probability. For each side row it
uses the market ask as the prior:

    logit(p_win) = logit(market_ask) + alpha(features)

That keeps the market as the baseline and asks the model to learn when our
state-value stack should disagree. This script is analysis-only and does not
write live runtime artifacts.

Inputs:
  data/analysis_output/side_neutral_opportunities/side_neutral_opportunities.jsonl

Outputs:
  data/analysis_output/market_anchored_alpha/
    market_anchored_alpha_report.json
    market_anchored_alpha_model.json
    market_anchored_alpha_predictions.jsonl
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple


PROJECT_DIR = Path(__file__).resolve().parents[2]
ANALYSIS_DIR = PROJECT_DIR / "scripts" / "analysis"
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))
if str(ANALYSIS_DIR) not in sys.path:
    sys.path.insert(0, str(ANALYSIS_DIR))

import train_baseline_models as tbm  # noqa: E402
from scripts.trading.scoring_path_features import SCORING_PATH_MODEL_FIELD_KEYS  # noqa: E402


LOGGER = logging.getLogger("train_market_anchored_alpha")

DEFAULT_TABLE_PATH = (
    PROJECT_DIR
    / "data"
    / "analysis_output"
    / "side_neutral_opportunities"
    / "side_neutral_opportunities.jsonl"
)
DEFAULT_OUTPUT_ROOT = PROJECT_DIR / "data" / "analysis_output" / "market_anchored_alpha"
DEFAULT_MAX_ITER = 200

FEATURE_COLUMNS = [
    "side",
    "line",
    "inning",
    "inning_state",
    "outs",
    "balls",
    "strikes",
    "runners_on",
    "away_score",
    "home_score",
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
    "over_mid_no_vig",
    "under_mid_no_vig",
    "raw_model_probability",
    "raw_model_edge_to_ask",
    "raw_model_edge_to_mid_no_vig",
    "model_market_logit_residual",
    "fair_over_base_poisson",
    "fair_over_base_empirical",
    "stage2_run_env_delta",
    "team_offense_delta",
    "fv_used_fallback",
    "fv_state_fallback_level",
    "fv_state_fallback_label",
    "fv_line_fallback_mode",
    "best_side_by_edge",
    "best_edge_to_ask",
]


@dataclass
class AlphaModel:
    bias: float
    weights: List[float]
    is_constant: bool = False
    alpha_constant: Optional[float] = None
    l2: Optional[float] = None
    lr: Optional[float] = None
    iterations: Optional[int] = None
    train_loss: Optional[float] = None


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Train market-anchored alpha model from side-neutral rows.")
    p.add_argument("--table-path", type=Path, default=DEFAULT_TABLE_PATH)
    p.add_argument("--min-date", type=str, default="", help="Inclusive YYYY-MM-DD.")
    p.add_argument("--max-date", type=str, default="", help="Inclusive YYYY-MM-DD.")
    p.add_argument("--side", choices=["both", "over", "under"], default="both")
    p.add_argument("--val-frac", type=float, default=0.20)
    p.add_argument("--test-frac", type=float, default=0.20)
    p.add_argument("--min-train-rows", type=int, default=30)
    p.add_argument("--max-iter", type=int, default=DEFAULT_MAX_ITER)
    p.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    p.add_argument("--strict", action="store_true")
    p.add_argument("--verbose", action="store_true")
    return p.parse_args(argv)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _safe_float(value: Any) -> Optional[float]:
    try:
        if value is None or value == "":
            return None
        out = float(value)
        if not math.isfinite(out):
            return None
        return out
    except Exception:
        return None


def _label_int(value: Any) -> Optional[int]:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return 1 if value else 0
    if isinstance(value, (int, float)):
        return 1 if int(value) == 1 else 0
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "y"}:
        return 1
    if text in {"0", "false", "no", "n"}:
        return 0
    return None


def _clip_prob(value: float) -> float:
    return max(1e-6, min(1.0 - 1e-6, float(value)))


def _logit(value: float) -> float:
    p = _clip_prob(value)
    return math.log(p / (1.0 - p))


def _sigmoid(value: float) -> float:
    if value >= 0:
        z = math.exp(-value)
        return 1.0 / (1.0 + z)
    z = math.exp(value)
    return z / (1.0 + z)


def _dot(a: Sequence[float], b: Sequence[float]) -> float:
    return sum(x * y for x, y in zip(a, b))


def _date_in_range(date_str: str, min_date: str, max_date: str) -> bool:
    if min_date and date_str < min_date:
        return False
    if max_date and date_str > max_date:
        return False
    return True


def read_jsonl(path: Path) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    with open(path, encoding="utf-8") as f:
        for raw in f:
            raw = raw.strip()
            if raw:
                rows.append(json.loads(raw))
    return rows


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


def build_split_map(dates: Sequence[str], val_frac: float, test_frac: float) -> Tuple[Dict[str, str], Dict[str, List[str]]]:
    unique = sorted(set(d for d in dates if d))
    train_n, val_n, test_n = _allocate_split_counts(len(unique), val_frac, test_frac)
    train_dates = unique[:train_n]
    val_dates = unique[train_n : train_n + val_n]
    test_dates = unique[train_n + val_n : train_n + val_n + test_n]
    split_map: Dict[str, str] = {}
    for d in train_dates:
        split_map[d] = "train"
    for d in val_dates:
        split_map[d] = "validation"
    for d in test_dates:
        split_map[d] = "test"
    return split_map, {"train": train_dates, "validation": val_dates, "test": test_dates}


def _side_row(row: Dict[str, Any], side: str, split: str) -> Optional[Dict[str, Any]]:
    if side == "over":
        market_price = _safe_float(row.get("over_ask"))
        market_mid = _safe_float(row.get("over_mid"))
        market_spread = _safe_float(row.get("over_spread"))
        opposite_price = _safe_float(row.get("under_ask"))
        raw_model_probability = _safe_float(row.get("fair_over"))
        raw_model_edge = _safe_float(row.get("over_edge_to_ask"))
        raw_model_mid_edge = _safe_float(row.get("over_edge_to_mid_no_vig"))
        residual = _safe_float(row.get("over_market_logit_residual"))
        target_win = _label_int(row.get("target_over_win"))
    else:
        market_price = _safe_float(row.get("under_ask"))
        market_mid = _safe_float(row.get("under_mid"))
        market_spread = _safe_float(row.get("under_spread"))
        opposite_price = _safe_float(row.get("over_ask"))
        raw_model_probability = _safe_float(row.get("fair_under"))
        raw_model_edge = _safe_float(row.get("under_edge_to_ask"))
        raw_model_mid_edge = _safe_float(row.get("under_edge_to_mid_no_vig"))
        residual = _safe_float(row.get("under_market_logit_residual"))
        target_win = _label_int(row.get("target_under_win"))

    if market_price is None or market_price <= 0 or market_price >= 1:
        return None
    if target_win is None:
        return None

    out: Dict[str, Any] = {
        "side_row_id": f"{row.get('row_id')}|{side}",
        "source_row_id": row.get("row_id"),
        "session_date": row.get("session_date"),
        "split": split,
        "side": side,
        "target_win": target_win,
        "market_price": market_price,
        "market_logit": _logit(market_price),
        "market_mid": market_mid,
        "market_spread": market_spread,
        "opposite_market_price": opposite_price,
        "raw_model_probability": raw_model_probability,
        "raw_model_edge_to_ask": raw_model_edge,
        "raw_model_edge_to_mid_no_vig": raw_model_mid_edge,
        "model_market_logit_residual": residual,
    }
    passthrough = [
        "game_pk",
        "away_abbrev",
        "home_abbrev",
        "line",
        "inning",
        "inning_state",
        "outs",
        "balls",
        "strikes",
        "runners_on",
        "away_score",
        "home_score",
        "current_total",
        "home_leading_late",
        "batting_team_is_home",
        "bottom9_available_if_needed",
        "expected_remaining_half_innings",
        "expected_remaining_pa_bucket",
        "home_skip_bottom9_risk",
        "over_under_ask_sum",
        "over_under_bid_sum",
        "over_mid_no_vig",
        "under_mid_no_vig",
        "fair_over_base_poisson",
        "fair_over_base_empirical",
        "stage2_run_env_delta",
        "team_offense_delta",
        "fv_used_fallback",
        "fv_state_fallback_level",
        "fv_state_fallback_label",
        "fv_line_fallback_mode",
        "best_side_by_edge",
        "best_edge_to_ask",
    ]
    for key in passthrough:
        out[key] = row.get(key)
    return out


def build_side_training_rows(
    opportunity_rows: Sequence[Dict[str, Any]],
    *,
    side: str = "both",
    min_date: str = "",
    max_date: str = "",
    val_frac: float = 0.20,
    test_frac: float = 0.20,
) -> Tuple[List[Dict[str, Any]], Dict[str, List[str]]]:
    filtered = [
        r
        for r in opportunity_rows
        if _date_in_range(str(r.get("session_date") or ""), min_date, max_date)
    ]
    split_map, split_dates = build_split_map(
        [str(r.get("session_date") or "") for r in filtered],
        val_frac=val_frac,
        test_frac=test_frac,
    )
    sides = ("over", "under") if side == "both" else (side,)
    out: List[Dict[str, Any]] = []
    for row in filtered:
        split = split_map.get(str(row.get("session_date") or ""), "train")
        for side_name in sides:
            side_row = _side_row(row, side_name, split)
            if side_row is not None:
                out.append(side_row)
    out.sort(key=lambda r: (str(r.get("session_date")), str(r.get("side_row_id"))))
    return out, split_dates


def fit_offset_logistic(
    X: List[List[float]],
    y: List[int],
    offsets: List[float],
    *,
    l2: float,
    lr: float,
    max_iter: int = 1500,
    tol: float = 1e-8,
) -> AlphaModel:
    n = len(X)
    p = len(X[0]) if X else 0
    if n == 0:
        raise ValueError("Cannot fit offset logistic model with zero rows.")

    bias = 0.0
    weights = [0.0] * p
    prev_loss: Optional[float] = None
    final_loss = 0.0

    for it in range(max_iter):
        grad_b = 0.0
        grad_w = [0.0] * p
        loss = 0.0
        for xi, yi, oi in zip(X, y, offsets):
            z = oi + bias + _dot(weights, xi)
            pi = _sigmoid(z)
            pi_clip = min(max(pi, 1e-12), 1.0 - 1e-12)
            loss += -(yi * math.log(pi_clip) + (1 - yi) * math.log(1.0 - pi_clip))
            diff = pi - yi
            grad_b += diff
            for j, xij in enumerate(xi):
                if xij:
                    grad_w[j] += diff * xij

        loss /= float(n)
        loss += 0.5 * l2 * sum(w * w for w in weights)
        final_loss = loss
        grad_b /= float(n)
        for j in range(p):
            grad_w[j] = grad_w[j] / float(n) + l2 * weights[j]

        lr_t = lr / math.sqrt(1.0 + 0.02 * it)
        bias -= lr_t * grad_b
        for j in range(p):
            weights[j] -= lr_t * grad_w[j]

        if prev_loss is not None and abs(prev_loss - loss) < tol:
            return AlphaModel(
                bias=bias,
                weights=weights,
                l2=l2,
                lr=lr,
                iterations=it + 1,
                train_loss=final_loss,
            )
        prev_loss = loss

    return AlphaModel(
        bias=bias,
        weights=weights,
        l2=l2,
        lr=lr,
        iterations=max_iter,
        train_loss=final_loss,
    )


def predict_offset(model: AlphaModel, X: List[List[float]], offsets: List[float]) -> List[float]:
    if model.is_constant:
        alpha = model.alpha_constant or 0.0
        return [_sigmoid(o + alpha) for o in offsets]
    return [_sigmoid(o + model.bias + _dot(model.weights, xi)) for xi, o in zip(X, offsets)]


def _dataset(rows: List[Dict[str, Any]], prep: tbm.Preprocessor) -> Tuple[List[List[float]], List[int], List[float]]:
    X = tbm.transform_rows(rows, prep)
    y = [int(r.get("target_win")) for r in rows]
    offsets = [_logit(float(r.get("market_price"))) for r in rows]
    return X, y, offsets


def _subset_metrics(rows: List[Dict[str, Any]], probs: List[float]) -> Dict[str, Any]:
    y = [int(r.get("target_win")) for r in rows]
    market = [float(r.get("market_price")) for r in rows]
    raw = [
        _safe_float(r.get("raw_model_probability"))
        if _safe_float(r.get("raw_model_probability")) is not None
        else float(r.get("market_price"))
        for r in rows
    ]
    out = {
        "rows": len(rows),
        "market": tbm.metric_summary(y, market),
        "raw_model": tbm.metric_summary(y, [float(x) for x in raw]),
        "market_anchored_alpha": tbm.metric_summary(y, probs),
    }
    return out


def _profit_summary(rows: Sequence[Dict[str, Any]], probs: Sequence[float], min_alpha_edge: float) -> Dict[str, Any]:
    selected: List[Tuple[Dict[str, Any], float]] = []
    for row, prob in zip(rows, probs):
        market = _safe_float(row.get("market_price"))
        if market is None:
            continue
        if prob - market >= min_alpha_edge:
            selected.append((row, prob))
    profit = 0.0
    stake = 0.0
    wins = 0
    for row, _prob in selected:
        price = float(row["market_price"])
        won = int(row["target_win"]) == 1
        stake += 1.0
        if won:
            wins += 1
            profit += (1.0 / price) - 1.0
        else:
            profit -= 1.0
    return {
        "min_alpha_edge": min_alpha_edge,
        "selected_rows": len(selected),
        "wins": wins,
        "losses": len(selected) - wins,
        "win_rate": wins / len(selected) if selected else None,
        "profit_units": profit,
        "roi": profit / stake if stake else None,
    }


def train_alpha_model(
    rows: List[Dict[str, Any]],
    *,
    feature_cols: Sequence[str] = FEATURE_COLUMNS,
    min_train_rows: int = 30,
    max_iter: int = DEFAULT_MAX_ITER,
    strict: bool = False,
) -> Tuple[Dict[str, Any], Dict[str, Any], List[Dict[str, Any]]]:
    split_map: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for row in rows:
        split_map[str(row.get("split") or "train")].append(row)
    train_rows = split_map.get("train", [])
    val_rows = split_map.get("validation", [])
    test_rows = split_map.get("test", [])

    if strict and len(train_rows) < min_train_rows:
        raise SystemExit(f"Strict mode failed: train rows {len(train_rows)} < {min_train_rows}.")
    if strict and len(set(int(r.get("target_win")) for r in train_rows)) < 2:
        raise SystemExit("Strict mode failed: train rows contain only one class.")
    if not train_rows:
        raise SystemExit("No train rows available.")

    prep = tbm.fit_preprocessor(train_rows, list(feature_cols))
    X_train, y_train, off_train = _dataset(train_rows, prep)
    X_val, y_val, off_val = _dataset(val_rows, prep)
    X_test, y_test, off_test = _dataset(test_rows, prep)

    search_grid = [(l2, lr) for l2 in [0.1, 1.0] for lr in [0.05]]
    best_model: Optional[AlphaModel] = None
    best_score: Optional[float] = None
    best_meta: Dict[str, Any] = {}
    for l2, lr in search_grid:
        model = fit_offset_logistic(
            X_train,
            y_train,
            off_train,
            l2=l2,
            lr=lr,
            max_iter=max_iter,
        )
        if val_rows:
            p_val = predict_offset(model, X_val, off_val)
            score = tbm._logloss(y_val, p_val)
        else:
            p_train = predict_offset(model, X_train, off_train)
            score = tbm._logloss(y_train, p_train)
        if score is None:
            continue
        if best_score is None or score < best_score:
            best_score = score
            best_model = model
            best_meta = {"l2": l2, "lr": lr, "selection_metric": score}
    if best_model is None:
        raise SystemExit("Failed to fit market-anchored alpha model.")

    p_train = predict_offset(best_model, X_train, off_train)
    p_val = predict_offset(best_model, X_val, off_val)
    p_test = predict_offset(best_model, X_test, off_test)

    pred_rows: List[Dict[str, Any]] = []
    for split_name, split_rows, probs in (
        ("train", train_rows, p_train),
        ("validation", val_rows, p_val),
        ("test", test_rows, p_test),
    ):
        for row, prob in zip(split_rows, probs):
            market = float(row["market_price"])
            pred_rows.append(
                {
                    "side_row_id": row.get("side_row_id"),
                    "source_row_id": row.get("source_row_id"),
                    "session_date": row.get("session_date"),
                    "split": split_name,
                    "side": row.get("side"),
                    "target_win": row.get("target_win"),
                    "market_price": market,
                    "raw_model_probability": row.get("raw_model_probability"),
                    "alpha_probability": prob,
                    "alpha_edge_to_market": prob - market,
                    "alpha_logit_residual": _logit(prob) - _logit(market),
                }
            )
    pred_rows.sort(key=lambda r: (str(r.get("session_date")), str(r.get("side_row_id"))))

    metrics = {
        "train": _subset_metrics(train_rows, p_train),
        "validation": _subset_metrics(val_rows, p_val),
        "test": _subset_metrics(test_rows, p_test),
    }
    profit = {
        "train": [_profit_summary(train_rows, p_train, th) for th in (0.0, 0.02, 0.05, 0.10)],
        "validation": [_profit_summary(val_rows, p_val, th) for th in (0.0, 0.02, 0.05, 0.10)],
        "test": [_profit_summary(test_rows, p_test, th) for th in (0.0, 0.02, 0.05, 0.10)],
    }
    top_coefficients = [
        {"feature": name, "weight": weight}
        for name, weight in sorted(
            zip(prep.feature_names, best_model.weights),
            key=lambda pair: abs(pair[1]),
            reverse=True,
        )[:20]
    ]
    model_payload = {
        "schema_version": 1,
        "task": "market_anchored_alpha",
        "fixed_offset": "logit(market_price)",
        "label_col": "target_win",
        "selected_feature_columns": list(feature_cols),
        "search_selected": best_meta,
        "model": {
            "bias": best_model.bias,
            "weights": best_model.weights,
            "l2": best_model.l2,
            "lr": best_model.lr,
            "iterations": best_model.iterations,
            "train_loss": best_model.train_loss,
        },
        "preprocessor": {
            "numeric_cols": prep.numeric_cols,
            "categorical_cols": prep.categorical_cols,
            "medians": prep.medians,
            "means": prep.means,
            "stds": prep.stds,
            "categories": prep.categories,
            "feature_names": prep.feature_names,
        },
        "weights": [
            {"feature": name, "weight": weight}
            for name, weight in zip(prep.feature_names, best_model.weights)
        ],
        "top_coefficients": top_coefficients,
        "metrics": metrics,
    }
    report = {
        "generated_at_utc": _now_iso(),
        "description": "Market-anchored alpha model: logit(p)=logit(market_ask)+alpha(features).",
        "rows": {
            "train": len(train_rows),
            "validation": len(val_rows),
            "test": len(test_rows),
            "total": len(rows),
        },
        "by_side": dict(sorted(Counter(str(r.get("side")) for r in rows).items())),
        "feature_count_numeric": len(prep.numeric_cols),
        "feature_count_categorical": sum(len(prep.categories[c]) for c in prep.categorical_cols),
        "feature_count_total": len(prep.feature_names),
        "search_selected": best_meta,
        "max_iter": max_iter,
        "metrics": metrics,
        "profit_policy_sweeps": profit,
        "top_coefficients": top_coefficients,
        "warnings": [
            "This is a research artifact, not a live runtime artifact.",
            "When side=both, Over and Under rows from one state are complements and should not be read as independent samples.",
        ],
    }
    return report, model_payload, pred_rows


def _write_json(path: Path, payload: Dict[str, Any]) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)


def _write_jsonl(path: Path, rows: Iterable[Dict[str, Any]]) -> None:
    with open(path, "w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row) + "\n")


def main(argv: Optional[List[str]] = None) -> None:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s %(message)s",
    )
    if not args.table_path.exists():
        raise SystemExit(f"Missing table path: {args.table_path}")
    opportunity_rows = read_jsonl(args.table_path)
    side_rows, split_dates = build_side_training_rows(
        opportunity_rows,
        side=args.side,
        min_date=args.min_date,
        max_date=args.max_date,
        val_frac=args.val_frac,
        test_frac=args.test_frac,
    )
    if args.strict and not side_rows:
        raise SystemExit("Strict mode failed: no labeled side rows.")

    report, model_payload, pred_rows = train_alpha_model(
        side_rows,
        min_train_rows=args.min_train_rows,
        max_iter=args.max_iter,
        strict=args.strict,
    )
    report["config"] = {
        "table_path": str(args.table_path),
        "min_date": args.min_date or None,
        "max_date": args.max_date or None,
        "side": args.side,
        "val_frac": args.val_frac,
        "test_frac": args.test_frac,
        "min_train_rows": args.min_train_rows,
        "max_iter": args.max_iter,
    }
    report["split_dates"] = split_dates

    args.output_root.mkdir(parents=True, exist_ok=True)
    report_path = args.output_root / "market_anchored_alpha_report.json"
    model_path = args.output_root / "market_anchored_alpha_model.json"
    pred_path = args.output_root / "market_anchored_alpha_predictions.jsonl"
    _write_json(report_path, report)
    _write_json(model_path, model_payload)
    _write_jsonl(pred_path, pred_rows)
    LOGGER.info("Wrote %s", report_path)
    LOGGER.info("Wrote %s", model_path)
    LOGGER.info("Wrote %s", pred_path)


if __name__ == "__main__":
    main()
