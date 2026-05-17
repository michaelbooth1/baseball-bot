#!/usr/bin/env python3
"""
Train baseline binary models on the leakage-aware signal training table.

Models trained:
  1) signal_win      -> target_win using pre_signal_features only
  2) execution_fill  -> target_filled using pre_signal + post_signal features

Inputs:
  data/analysis_output/training_tables/signal_training_table.jsonl
  data/analysis_output/training_tables/signal_training_table_manifest.json

Outputs:
  data/analysis_output/model_baselines/
    baseline_model_report.json
    signal_win_model.json
    execution_fill_model.json
    signal_win_predictions.jsonl
    execution_fill_predictions.jsonl
"""

from __future__ import annotations

import argparse
import json
import logging
import math
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple


PROJECT_DIR = Path(__file__).resolve().parents[2]
DEFAULT_TABLE_PATH = PROJECT_DIR / "data" / "analysis_output" / "training_tables" / "signal_training_table.jsonl"
DEFAULT_MANIFEST_PATH = (
    PROJECT_DIR / "data" / "analysis_output" / "training_tables" / "signal_training_table_manifest.json"
)
DEFAULT_OUTPUT_ROOT = PROJECT_DIR / "data" / "analysis_output" / "model_baselines"

LOGGER = logging.getLogger("train_baseline_models")


@dataclass
class Preprocessor:
    numeric_cols: List[str]
    categorical_cols: List[str]
    medians: Dict[str, float]
    means: Dict[str, float]
    stds: Dict[str, float]
    categories: Dict[str, List[str]]
    feature_names: List[str]


@dataclass
class LogisticModel:
    bias: float
    weights: List[float]
    is_constant: bool = False
    constant_prob: Optional[float] = None
    l2: Optional[float] = None
    lr: Optional[float] = None
    iterations: Optional[int] = None
    train_loss: Optional[float] = None


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Train baseline models on signal_training_table.")
    p.add_argument(
        "--table-path",
        type=Path,
        default=DEFAULT_TABLE_PATH,
        help=f"Path to signal_training_table JSONL (default: {DEFAULT_TABLE_PATH}).",
    )
    p.add_argument(
        "--manifest-path",
        type=Path,
        default=DEFAULT_MANIFEST_PATH,
        help=f"Path to signal_training_table manifest JSON (default: {DEFAULT_MANIFEST_PATH}).",
    )
    p.add_argument(
        "--output-root",
        type=Path,
        default=DEFAULT_OUTPUT_ROOT,
        help=f"Output directory (default: {DEFAULT_OUTPUT_ROOT}).",
    )
    p.add_argument("--strict", action="store_true", help="Fail if train split has no rows or labels.")
    p.add_argument("--verbose", action="store_true", help="Verbose logging.")
    return p.parse_args(argv)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _read_json(path: Path) -> Dict[str, Any]:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _read_jsonl(path: Path) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    with open(path, encoding="utf-8") as f:
        for raw in f:
            raw = raw.strip()
            if not raw:
                continue
            rows.append(json.loads(raw))
    return rows


def _write_json(path: Path, payload: Dict[str, Any]) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)


def _write_jsonl(path: Path, rows: Iterable[Dict[str, Any]]) -> None:
    with open(path, "w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row) + "\n")


def _is_missing(v: Any) -> bool:
    return v is None or v == ""


def _to_float(v: Any) -> Optional[float]:
    if _is_missing(v):
        return None
    if isinstance(v, bool):
        return 1.0 if v else 0.0
    if isinstance(v, (int, float)):
        fv = float(v)
        if not math.isfinite(fv):
            return None
        return fv
    s = str(v).strip().lower()
    if not s:
        return None
    if s in {"true", "yes", "y"}:
        return 1.0
    if s in {"false", "no", "n"}:
        return 0.0
    try:
        fv = float(s)
        if not math.isfinite(fv):
            return None
        return fv
    except Exception:
        return None


def _stable_sigmoid(x: float) -> float:
    if x >= 0:
        z = math.exp(-x)
        return 1.0 / (1.0 + z)
    z = math.exp(x)
    return z / (1.0 + z)


def _logloss(y: Sequence[int], p: Sequence[float]) -> Optional[float]:
    if not y:
        return None
    eps = 1e-12
    total = 0.0
    for yi, pi in zip(y, p):
        p_clip = min(max(pi, eps), 1.0 - eps)
        total += -(yi * math.log(p_clip) + (1 - yi) * math.log(1.0 - p_clip))
    return total / float(len(y))


def _brier(y: Sequence[int], p: Sequence[float]) -> Optional[float]:
    if not y:
        return None
    return sum((yi - pi) ** 2 for yi, pi in zip(y, p)) / float(len(y))


def _accuracy(y: Sequence[int], p: Sequence[float], threshold: float = 0.5) -> Optional[float]:
    if not y:
        return None
    correct = sum(1 for yi, pi in zip(y, p) if (1 if pi >= threshold else 0) == yi)
    return correct / float(len(y))


def _auc_pairwise(y: Sequence[int], p: Sequence[float]) -> Optional[float]:
    if not y:
        return None
    pos = [pi for yi, pi in zip(y, p) if yi == 1]
    neg = [pi for yi, pi in zip(y, p) if yi == 0]
    if not pos or not neg:
        return None
    score = 0.0
    for pp in pos:
        for pn in neg:
            if pp > pn:
                score += 1.0
            elif pp == pn:
                score += 0.5
    return score / float(len(pos) * len(neg))


def _feature_summary(train_rows: List[Dict[str, Any]], feature_cols: List[str]) -> Tuple[List[str], List[str]]:
    numeric_candidates: List[str] = []
    categorical_candidates: List[str] = []
    for col in feature_cols:
        non_null = [row.get(col) for row in train_rows if not _is_missing(row.get(col))]
        if not non_null:
            continue
        num_parsed = [v for v in (_to_float(x) for x in non_null) if v is not None]
        numeric_ratio = len(num_parsed) / float(len(non_null))
        if numeric_ratio >= 0.80:
            numeric_candidates.append(col)
        else:
            categorical_candidates.append(col)
    return numeric_candidates, categorical_candidates


def fit_preprocessor(
    train_rows: List[Dict[str, Any]],
    feature_cols: List[str],
    min_cat_freq: int = 2,
    max_categories: int = 16,
) -> Preprocessor:
    numeric_candidates, categorical_candidates = _feature_summary(train_rows, feature_cols)

    numeric_cols: List[str] = []
    medians: Dict[str, float] = {}
    means: Dict[str, float] = {}
    stds: Dict[str, float] = {}
    feature_names: List[str] = []

    for col in numeric_candidates:
        parsed_vals = [_to_float(row.get(col)) for row in train_rows]
        finite_vals = [v for v in parsed_vals if v is not None]
        if len(finite_vals) < 2:
            continue
        sorted_vals = sorted(finite_vals)
        mid = len(sorted_vals) // 2
        if len(sorted_vals) % 2 == 1:
            median = sorted_vals[mid]
        else:
            median = (sorted_vals[mid - 1] + sorted_vals[mid]) / 2.0
        filled = [v if v is not None else median for v in parsed_vals]
        uniq = len(set(round(v, 10) for v in filled))
        if uniq < 2:
            continue
        mean = sum(filled) / float(len(filled))
        variance = sum((v - mean) ** 2 for v in filled) / float(len(filled))
        std = math.sqrt(max(variance, 0.0))
        if std < 1e-12:
            continue
        numeric_cols.append(col)
        medians[col] = median
        means[col] = mean
        stds[col] = std
        feature_names.append(f"num::{col}")

    categorical_cols: List[str] = []
    categories: Dict[str, List[str]] = {}
    for col in categorical_candidates:
        vals = [str(row.get(col)).strip() for row in train_rows if not _is_missing(row.get(col))]
        if len(vals) < 2:
            continue
        counts = Counter(vals)
        cats = sorted([k for k, c in counts.items() if c >= min_cat_freq])
        if len(cats) < 2 or len(cats) > max_categories:
            continue
        categorical_cols.append(col)
        categories[col] = cats
        for cat in cats:
            feature_names.append(f"cat::{col}=={cat}")

    return Preprocessor(
        numeric_cols=numeric_cols,
        categorical_cols=categorical_cols,
        medians=medians,
        means=means,
        stds=stds,
        categories=categories,
        feature_names=feature_names,
    )


def transform_rows(rows: List[Dict[str, Any]], prep: Preprocessor) -> List[List[float]]:
    out: List[List[float]] = []
    for row in rows:
        vec: List[float] = []
        for col in prep.numeric_cols:
            val = _to_float(row.get(col))
            if val is None:
                val = prep.medians[col]
            z = (val - prep.means[col]) / prep.stds[col]
            vec.append(z)
        for col in prep.categorical_cols:
            sval = "" if _is_missing(row.get(col)) else str(row.get(col)).strip()
            for cat in prep.categories[col]:
                vec.append(1.0 if sval == cat else 0.0)
        out.append(vec)
    return out


def _dot(a: Sequence[float], b: Sequence[float]) -> float:
    return sum(x * y for x, y in zip(a, b))


def fit_logistic_regression(
    X: List[List[float]],
    y: List[int],
    l2: float,
    lr: float,
    max_iter: int = 1500,
    tol: float = 1e-8,
) -> LogisticModel:
    n = len(X)
    p = len(X[0]) if X else 0
    if n == 0:
        raise ValueError("Cannot fit logistic regression with zero rows.")
    if p == 0:
        mean_y = sum(y) / float(len(y))
        return LogisticModel(bias=0.0, weights=[], is_constant=True, constant_prob=mean_y, l2=l2, lr=lr, iterations=0)

    positive_rate = sum(y) / float(n)
    positive_rate = min(max(positive_rate, 1e-6), 1.0 - 1e-6)
    bias = math.log(positive_rate / (1.0 - positive_rate))
    weights = [0.0] * p
    prev_loss: Optional[float] = None
    final_loss: float = 0.0

    for it in range(max_iter):
        grad_b = 0.0
        grad_w = [0.0] * p
        loss = 0.0
        for xi, yi in zip(X, y):
            z = bias + _dot(weights, xi)
            pi = _stable_sigmoid(z)
            pi_clip = min(max(pi, 1e-12), 1.0 - 1e-12)
            loss += -(yi * math.log(pi_clip) + (1 - yi) * math.log(1.0 - pi_clip))
            diff = pi - yi
            grad_b += diff
            for j, xij in enumerate(xi):
                if xij != 0.0:
                    grad_w[j] += diff * xij

        loss /= float(n)
        reg = 0.5 * l2 * sum(w * w for w in weights)
        loss += reg
        final_loss = loss

        grad_b /= float(n)
        for j in range(p):
            grad_w[j] = grad_w[j] / float(n) + l2 * weights[j]

        lr_t = lr / math.sqrt(1.0 + (0.02 * it))
        bias -= lr_t * grad_b
        for j in range(p):
            weights[j] -= lr_t * grad_w[j]

        if prev_loss is not None and abs(prev_loss - loss) < tol:
            return LogisticModel(
                bias=bias,
                weights=weights,
                is_constant=False,
                l2=l2,
                lr=lr,
                iterations=it + 1,
                train_loss=final_loss,
            )
        prev_loss = loss

    return LogisticModel(
        bias=bias,
        weights=weights,
        is_constant=False,
        l2=l2,
        lr=lr,
        iterations=max_iter,
        train_loss=final_loss,
    )


def predict_probabilities(model: LogisticModel, X: List[List[float]]) -> List[float]:
    if model.is_constant:
        p = min(max(model.constant_prob or 0.5, 1e-8), 1.0 - 1e-8)
        return [p for _ in X]
    return [_stable_sigmoid(model.bias + _dot(model.weights, xi)) for xi in X]


def metric_summary(y: List[int], p: List[float]) -> Dict[str, Any]:
    return {
        "rows": len(y),
        "positive_rate": (sum(y) / float(len(y))) if y else None,
        "logloss": _logloss(y, p),
        "brier": _brier(y, p),
        "auc": _auc_pairwise(y, p),
        "accuracy_0p5": _accuracy(y, p, threshold=0.5),
    }


def _rows_for_task(rows: List[Dict[str, Any]], label_col: str) -> List[Dict[str, Any]]:
    out = []
    for row in rows:
        y = row.get(label_col)
        if y is None:
            continue
        out.append(row)
    return out


def _split_rows(rows: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    buckets: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for row in rows:
        split = str(row.get("split") or "train")
        buckets[split].append(row)
    return buckets


def _label_int(v: Any) -> int:
    if isinstance(v, bool):
        return 1 if v else 0
    if isinstance(v, (int, float)):
        return 1 if int(v) == 1 else 0
    s = str(v).strip().lower()
    return 1 if s in {"1", "true", "yes", "y"} else 0


def _dataset_xy(rows: List[Dict[str, Any]], prep: Preprocessor, label_col: str) -> Tuple[List[List[float]], List[int]]:
    X = transform_rows(rows, prep)
    y = [_label_int(r.get(label_col)) for r in rows]
    return X, y


def _top_coefficients(model: LogisticModel, prep: Preprocessor, top_n: int = 15) -> List[Dict[str, Any]]:
    if model.is_constant:
        return []
    pairs = list(zip(prep.feature_names, model.weights))
    pairs.sort(key=lambda x: abs(x[1]), reverse=True)
    out = []
    for name, weight in pairs[:top_n]:
        out.append({"feature": name, "weight": weight})
    return out


def _prediction_rows(
    rows: List[Dict[str, Any]],
    probs: List[float],
    label_col: str,
    task_name: str,
) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for row, prob in zip(rows, probs):
        out.append(
            {
                "task": task_name,
                "mode": row.get("mode"),
                "bet_id": row.get("bet_id"),
                "session_date": row.get("session_date"),
                "split": row.get("split"),
                "label": row.get(label_col),
                "prob": prob,
            }
        )
    return out


def train_task(
    all_rows: List[Dict[str, Any]],
    task_name: str,
    label_col: str,
    feature_cols: List[str],
    strict: bool,
) -> Tuple[Dict[str, Any], Dict[str, Any], List[Dict[str, Any]]]:
    labeled_rows = _rows_for_task(all_rows, label_col=label_col)
    split_map = _split_rows(labeled_rows)
    train_rows = split_map.get("train", [])
    val_rows = split_map.get("validation", [])
    test_rows = split_map.get("test", [])

    if strict and not train_rows:
        raise SystemExit(f"{task_name}: strict mode failed, no train rows with label {label_col}.")
    if strict and len(set(_label_int(r.get(label_col)) for r in train_rows)) < 2:
        raise SystemExit(f"{task_name}: strict mode failed, train rows for {label_col} contain only one class.")

    prep = fit_preprocessor(train_rows=train_rows, feature_cols=feature_cols)
    X_train, y_train = _dataset_xy(train_rows, prep, label_col)
    X_val, y_val = _dataset_xy(val_rows, prep, label_col)
    X_test, y_test = _dataset_xy(test_rows, prep, label_col)

    if strict and (not X_train or len(X_train[0]) == 0):
        raise SystemExit(f"{task_name}: strict mode failed, no usable features after preprocessing.")

    search_grid = [(l2, lr) for l2 in [0.01, 0.05, 0.1, 0.5, 1.0, 2.0] for lr in [0.03, 0.05, 0.1]]
    best_model: Optional[LogisticModel] = None
    best_score: Optional[float] = None
    best_meta: Dict[str, Any] = {}

    for l2, lr in search_grid:
        model = fit_logistic_regression(X_train, y_train, l2=l2, lr=lr)
        if y_val:
            p_val = predict_probabilities(model, X_val)
            score = _logloss(y_val, p_val)
        else:
            p_train = predict_probabilities(model, X_train)
            score = _logloss(y_train, p_train)
        if score is None:
            continue
        if best_score is None or score < best_score:
            best_score = score
            best_model = model
            best_meta = {"l2": l2, "lr": lr, "selection_metric": score}

    if best_model is None:
        raise SystemExit(f"{task_name}: failed to train model.")

    p_train = predict_probabilities(best_model, X_train)
    p_val = predict_probabilities(best_model, X_val)
    p_test = predict_probabilities(best_model, X_test)

    metrics = {
        "train": metric_summary(y_train, p_train),
        "validation": metric_summary(y_val, p_val),
        "test": metric_summary(y_test, p_test),
    }

    prediction_rows = (
        _prediction_rows(train_rows, p_train, label_col=label_col, task_name=task_name)
        + _prediction_rows(val_rows, p_val, label_col=label_col, task_name=task_name)
        + _prediction_rows(test_rows, p_test, label_col=label_col, task_name=task_name)
    )
    prediction_rows.sort(key=lambda r: (str(r.get("session_date") or ""), str(r.get("split") or ""), str(r.get("bet_id") or "")))

    model_payload = {
        "task": task_name,
        "label_col": label_col,
        "selected_feature_columns": feature_cols,
        "search_selected": best_meta,
        "model": {
            "is_constant": best_model.is_constant,
            "bias": best_model.bias,
            "constant_prob": best_model.constant_prob,
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
        "top_coefficients": _top_coefficients(best_model, prep),
        "metrics": metrics,
        "rows": {
            "train": len(train_rows),
            "validation": len(val_rows),
            "test": len(test_rows),
            "labeled_total": len(labeled_rows),
        },
    }

    summary = {
        "task": task_name,
        "label_col": label_col,
        "rows": model_payload["rows"],
        "feature_count_numeric": len(prep.numeric_cols),
        "feature_count_categorical": sum(len(prep.categories[c]) for c in prep.categorical_cols),
        "feature_count_total": len(prep.feature_names),
        "search_selected": best_meta,
        "metrics": metrics,
        "top_coefficients": model_payload["top_coefficients"],
    }
    return summary, model_payload, prediction_rows


def main(argv: Optional[List[str]] = None) -> None:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s %(message)s",
    )

    if not args.table_path.exists():
        raise SystemExit(f"Missing table path: {args.table_path}")
    if not args.manifest_path.exists():
        raise SystemExit(f"Missing manifest path: {args.manifest_path}")
    args.output_root.mkdir(parents=True, exist_ok=True)

    rows = _read_jsonl(args.table_path)
    manifest = _read_json(args.manifest_path)
    groups = manifest.get("column_groups", {})
    pre_signal = list(groups.get("pre_signal_features", []))
    post_signal = list(groups.get("post_signal_execution_features", []))
    if args.strict and not pre_signal:
        raise SystemExit("Strict mode failed: pre_signal_features missing from manifest.")

    tasks = [
        {
            "task_name": "signal_win",
            "label_col": "target_win",
            "feature_cols": pre_signal,
        },
        {
            "task_name": "execution_fill",
            "label_col": "target_filled",
            "feature_cols": pre_signal + post_signal,
        },
    ]

    report_tasks: List[Dict[str, Any]] = []
    for task in tasks:
        summary, model_payload, prediction_rows = train_task(
            all_rows=rows,
            task_name=task["task_name"],
            label_col=task["label_col"],
            feature_cols=task["feature_cols"],
            strict=args.strict,
        )
        report_tasks.append(summary)

        model_path = args.output_root / f"{task['task_name']}_model.json"
        pred_path = args.output_root / f"{task['task_name']}_predictions.jsonl"
        _write_json(model_path, model_payload)
        _write_jsonl(pred_path, prediction_rows)
        LOGGER.info("Wrote %s", model_path)
        LOGGER.info("Wrote %s", pred_path)

    report = {
        "generated_at_utc": _now_iso(),
        "config": {
            "table_path": str(args.table_path),
            "manifest_path": str(args.manifest_path),
            "output_root": str(args.output_root),
            "strict": bool(args.strict),
        },
        "tasks": report_tasks,
        "notes": {
            "signal_win": "Uses only pre_signal_features to avoid post-signal leakage.",
            "execution_fill": "Uses pre_signal_features + post_signal_execution_features.",
            "training_rule": "Models fit on train split; hyperparameters selected by validation logloss when available.",
        },
    }
    report_path = args.output_root / "baseline_model_report.json"
    _write_json(report_path, report)
    LOGGER.info("Wrote %s", report_path)


if __name__ == "__main__":
    main()
