#!/usr/bin/env python3
"""
Build and evaluate an EV-ranked trade policy from baseline models.

Workflow:
1) Retrain two logistic baselines with stricter fill feature policy:
   - p_win_if_filled: target_win on rows where target_filled == 1, pre-signal features
   - p_fill_runtime:  target_filled on all labeled rows, pre-signal decision-time features
   - p_fill_strict:   target_filled on all labeled rows, pre + post features excluding sim_* proxies
2) Score each row with:
   - ev_if_filled
   - ev_realized = p_fill_runtime * ev_if_filled
   - ev_per_stake
3) Tune policy on validation split over a simple grid:
   - min_ev_per_stake threshold
   - min_p_fill threshold
   - max trades per day cap
4) Report chosen policy and out-of-sample test metrics.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

PROJECT_DIR = Path(__file__).resolve().parents[2]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from scripts.analysis import train_baseline_models as tbm
from scripts.trading.model_families import KNOWN_MODEL_FAMILIES, SCORE_EVENT_TRANSITION, infer_signal_model_family


DEFAULT_TABLE_PATH = PROJECT_DIR / "data" / "analysis_output" / "training_tables" / "signal_training_table.jsonl"
DEFAULT_MANIFEST_PATH = (
    PROJECT_DIR / "data" / "analysis_output" / "training_tables" / "signal_training_table_manifest.json"
)
DEFAULT_OUTPUT_ROOT = PROJECT_DIR / "data" / "analysis_output" / "ev_policy"

LOGGER = logging.getLogger("backtest_ev_policy")

RUNTIME_FEATURE_DENY_EXACT = frozenset({
    # Identifiers/provenance that are available at decision time but invite
    # memorization or runtime-null churn rather than durable EV signal.
    "over_token_id",
    "weather_cache_date",
    "weather_selected_time_utc",
    "weather_source_error",
    "stadium_id",
    "stadium_primary_name",
    # Kelly columns are sizing diagnostics, not market/game-state features.
    # They are null in flat-stake live runs and should not be required by EV.
    "kelly_full_fraction",
    "kelly_fraction_used",
    "param_kelly_fraction",
    "param_kelly_max_bet_fraction",
    "param_kelly_max_edge",
    # Decision-time derived market field that requires both pair sides.
    # The runtime supplies entry_ask/decision_ask from the over book but
    # doesn't compute decision_mid (needs the bid at the exact decision
    # moment). Caught 2026-05-15 -- EV-policy retrain selected it and
    # the live engine logged the missing-features warning.
    "decision_mid",
})

RUNTIME_FEATURE_DENY_PREFIXES = (
    "sim_",
    # Book-pair-side fields. Even though the monitor polls both Over and
    # Under tokens and signal_engine.py attaches `under_best_bid/ask/...`
    # to the book payload, under_pair_available is only ~50% in
    # production (the under tick has to arrive in the same poll cycle
    # as the over tick; tick-timing variance often misses). When
    # under_pair_available=False, every under_* column lands as None
    # at decision time. Enforce-mode fail-closed safety dictates we
    # NOT let EV-policy pick features whose value is None ~half the
    # time; over_* fields are excluded by symmetry to keep the
    # book-pair family as one deny class. Phase A audit (2026-05-16)
    # confirmed the under data flows for OFFLINE analysis (training
    # tables, calibration, walk-forward); this deny prefix is a
    # RUNTIME safety, not an analysis safety. Phase C (market-maker
    # two-sided quoting) will need to raise the under-side coverage
    # before this prefix can be safely lifted.
    "over_",
    "under_",
)


@dataclass
class TrainedModel:
    preprocessor: tbm.Preprocessor
    model: tbm.LogisticModel
    feature_cols: List[str]
    label_col: str
    row_filter_name: str
    metrics: Dict[str, Any]
    search_selected: Dict[str, Any]


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Backtest EV-ranked policy using baseline models.")
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
    p.add_argument(
        "--policy-mode",
        choices=["live", "paper", "both"],
        default="live",
        help="Rows used for policy optimization/evaluation (default: live).",
    )
    p.add_argument(
        "--model-family",
        choices=["all", *KNOWN_MODEL_FAMILIES],
        default=SCORE_EVENT_TRANSITION,
        help=(
            "Model family to train/evaluate. Default keeps live EV policy on the "
            "score-event family instead of pooling with no-score drift."
        ),
    )
    p.add_argument(
        "--ev-thresholds",
        type=str,
        default="-0.20,-0.15,-0.10,-0.05,-0.02,0.00,0.01,0.02,0.03,0.05,0.08",
        help="Comma-separated min EV-per-stake thresholds for grid search.",
    )
    p.add_argument(
        "--pfill-thresholds",
        type=str,
        default="0.00,0.10,0.20,0.30,0.40",
        help="Comma-separated min p_fill thresholds for grid search.",
    )
    p.add_argument(
        "--max-per-day-options",
        type=str,
        default="0,1,2,3,5",
        help="Comma-separated per-day caps; 0 means no cap.",
    )
    p.add_argument(
        "--min-validation-trades",
        type=int,
        default=3,
        help="Minimum selected trades required on validation for policy candidacy.",
    )
    p.add_argument(
        "--artifact-purpose",
        choices=["evaluation", "runtime-refit"],
        default="evaluation",
        help=(
            "evaluation writes models fitted on the train split used for "
            "out-of-sample metrics. runtime-refit keeps validation/test "
            "evaluation and policy selection unchanged, then refits exported "
            "model artifacts on all eligible labeled rows with the selected "
            "hyperparameters."
        ),
    )
    p.add_argument("--strict", action="store_true", help="Fail on missing/invalid required inputs.")
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


def _parse_float_csv(raw: str, min_val: Optional[float] = None, max_val: Optional[float] = None) -> List[float]:
    out: List[float] = []
    for token in str(raw or "").split(","):
        token = token.strip()
        if not token:
            continue
        try:
            v = float(token)
        except Exception as exc:
            raise SystemExit(f"Invalid float list value '{token}': {exc}") from exc
        if min_val is not None and v < min_val:
            raise SystemExit(f"Value {v} is below minimum {min_val}")
        if max_val is not None and v > max_val:
            raise SystemExit(f"Value {v} is above maximum {max_val}")
        out.append(v)
    uniq = sorted(set(out))
    if not uniq:
        raise SystemExit("Expected at least one value in comma-separated list.")
    return uniq


def _parse_int_csv(raw: str, min_val: Optional[int] = None) -> List[int]:
    out: List[int] = []
    for token in str(raw or "").split(","):
        token = token.strip()
        if not token:
            continue
        try:
            v = int(token)
        except Exception as exc:
            raise SystemExit(f"Invalid int list value '{token}': {exc}") from exc
        if min_val is not None and v < min_val:
            raise SystemExit(f"Value {v} is below minimum {min_val}")
        out.append(v)
    uniq = sorted(set(out))
    if not uniq:
        raise SystemExit("Expected at least one integer value in comma-separated list.")
    return uniq


def _is_labeled(row: Dict[str, Any]) -> bool:
    return bool(row.get("label_available"))


def _label_int(v: Any) -> int:
    if isinstance(v, bool):
        return 1 if v else 0
    if isinstance(v, (int, float)):
        return 1 if int(v) == 1 else 0
    s = str(v).strip().lower()
    return 1 if s in {"1", "true", "yes", "y"} else 0


def _to_float(v: Any) -> Optional[float]:
    return tbm._to_float(v)  # shared helper


def _split_rows(rows: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    buckets: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for row in rows:
        buckets[str(row.get("split") or "train")].append(row)
    return buckets


def _filter_policy_rows(rows: List[Dict[str, Any]], policy_mode: str) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for row in rows:
        if not _is_labeled(row):
            continue
        mode = str(row.get("mode") or "")
        if policy_mode != "both" and mode != policy_mode:
            continue
        out.append(row)
    return out


def _filter_model_family_rows(rows: List[Dict[str, Any]], model_family: str) -> List[Dict[str, Any]]:
    if model_family == "all":
        return rows
    return [row for row in rows if infer_signal_model_family(row) == model_family]


def _feature_list_for_fill(pre_signal: List[str], post_signal: List[str]) -> List[str]:
    deny = re.compile(r"^sim_")
    post_filtered = [c for c in post_signal if not deny.match(c)]
    # Include mode as categorical context to help avoid paper/live confounding.
    # 'mode' is in identity_columns but not in feature groups.
    return pre_signal + post_filtered + ["mode"]


def _is_runtime_safe_feature_col(col: str) -> bool:
    if col in RUNTIME_FEATURE_DENY_EXACT:
        return False
    return not any(col.startswith(prefix) for prefix in RUNTIME_FEATURE_DENY_PREFIXES)


def _feature_list_for_runtime(pre_signal: List[str]) -> List[str]:
    """Reliable decision-time features for live/shadow EV runtime scoring."""
    return [c for c in pre_signal if _is_runtime_safe_feature_col(c)] + ["mode"]


def _feature_list_for_runtime_fill(pre_signal: List[str]) -> List[str]:
    """Decision-time safe fill features for live/shadow runtime scoring."""
    return _feature_list_for_runtime(pre_signal)


def _feature_list_for_win(pre_signal: List[str]) -> List[str]:
    """Decision-time safe win features for live/shadow runtime scoring."""
    return _feature_list_for_runtime(pre_signal)


def _model_artifact_payload(
    *,
    trained: TrainedModel,
    artifact_role: str,
    runtime_safe: bool,
    feature_policy: str,
    model_family: str,
    artifact_purpose: str,
    evaluation_metrics: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    return {
        "artifact_role": artifact_role,
        "artifact_purpose": artifact_purpose,
        "fit_scope": (
            "all_eligible_labeled_rows_after_hyperparameter_selection"
            if artifact_purpose == "runtime-refit"
            else "train_split"
        ),
        "selection_source": "train_validation_test_evaluation_split",
        "model_family": model_family,
        "runtime_safe": bool(runtime_safe),
        "feature_policy": feature_policy,
        "runtime_feature_exclusions": sorted(RUNTIME_FEATURE_DENY_EXACT) if runtime_safe else [],
        # Prefix-matched exclusions live separately so an operator
        # reading the artifact can audit BOTH the exact-name list and
        # the family-wide deny rules (added 2026-05-16; prevents the
        # confusion where the under_*/over_* book-pair family was
        # excluded but didn't appear in the visible exclusion list).
        "runtime_feature_exclusion_prefixes": list(RUNTIME_FEATURE_DENY_PREFIXES) if runtime_safe else [],
        "label_col": trained.label_col,
        "row_filter": trained.row_filter_name,
        "feature_cols_used": trained.feature_cols,
        "search_selected": trained.search_selected,
        "metrics": trained.metrics,
        "evaluation_metrics": evaluation_metrics or trained.metrics,
        "preprocessor": {
            "numeric_cols": trained.preprocessor.numeric_cols,
            "categorical_cols": trained.preprocessor.categorical_cols,
            "medians": trained.preprocessor.medians,
            "means": trained.preprocessor.means,
            "stds": trained.preprocessor.stds,
            "categories": trained.preprocessor.categories,
            "feature_names": trained.preprocessor.feature_names,
        },
        "model": {
            "is_constant": trained.model.is_constant,
            "bias": trained.model.bias,
            "constant_prob": trained.model.constant_prob,
            "l2": trained.model.l2,
            "lr": trained.model.lr,
            "iterations": trained.model.iterations,
            "train_loss": trained.model.train_loss,
        },
        "weights": [
            {"feature": fn, "weight": w}
            for fn, w in zip(trained.preprocessor.feature_names, trained.model.weights)
        ],
    }


def _rows_for_model(
    rows: List[Dict[str, Any]],
    label_col: str,
    filter_name: str,
) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for row in rows:
        if not _is_labeled(row):
            continue
        if row.get(label_col) is None:
            continue
        if filter_name == "filled_only" and _label_int(row.get("target_filled")) != 1:
            continue
        out.append(row)
    return out


def _dataset_xy(rows: List[Dict[str, Any]], prep: tbm.Preprocessor, label_col: str) -> Tuple[List[List[float]], List[int]]:
    X = tbm.transform_rows(rows, prep)
    y = [_label_int(r.get(label_col)) for r in rows]
    return X, y


def _metric_block(rows: List[Dict[str, Any]], probs: List[float], label_col: str) -> Dict[str, Any]:
    y = [_label_int(r.get(label_col)) for r in rows]
    return tbm.metric_summary(y, probs)


def train_binary_model(
    all_rows: List[Dict[str, Any]],
    label_col: str,
    feature_cols: List[str],
    row_filter_name: str,
    strict: bool,
) -> TrainedModel:
    rows = _rows_for_model(all_rows, label_col=label_col, filter_name=row_filter_name)
    by_split = _split_rows(rows)
    train_rows = by_split.get("train", [])
    val_rows = by_split.get("validation", [])
    test_rows = by_split.get("test", [])

    if strict and not train_rows:
        raise SystemExit(f"Strict mode failed: no train rows for {label_col}/{row_filter_name}.")

    if strict and len(set(_label_int(r.get(label_col)) for r in train_rows)) < 2:
        raise SystemExit(f"Strict mode failed: train rows for {label_col}/{row_filter_name} have one class.")

    prep = tbm.fit_preprocessor(train_rows=train_rows, feature_cols=feature_cols)
    X_train, y_train = _dataset_xy(train_rows, prep, label_col)
    X_val, y_val = _dataset_xy(val_rows, prep, label_col)
    X_test, y_test = _dataset_xy(test_rows, prep, label_col)

    if strict and (not X_train or len(X_train[0]) == 0):
        raise SystemExit(f"Strict mode failed: no usable features for {label_col}/{row_filter_name}.")

    grid = [(l2, lr) for l2 in [0.01, 0.05, 0.1, 0.5, 1.0, 2.0] for lr in [0.03, 0.05, 0.1]]
    best_model: Optional[tbm.LogisticModel] = None
    best_score: Optional[float] = None
    best_meta: Dict[str, Any] = {}

    for l2, lr in grid:
        model = tbm.fit_logistic_regression(X_train, y_train, l2=l2, lr=lr)
        if y_val:
            score = tbm._logloss(y_val, tbm.predict_probabilities(model, X_val))
        else:
            score = tbm._logloss(y_train, tbm.predict_probabilities(model, X_train))
        if score is None:
            continue
        if best_score is None or score < best_score:
            best_score = score
            best_model = model
            best_meta = {"l2": l2, "lr": lr, "selection_logloss": score}

    if best_model is None:
        raise SystemExit(f"Model fit failed for {label_col}/{row_filter_name}.")

    p_train = tbm.predict_probabilities(best_model, X_train)
    p_val = tbm.predict_probabilities(best_model, X_val)
    p_test = tbm.predict_probabilities(best_model, X_test)

    metrics = {
        "rows": {
            "total": len(rows),
            "train": len(train_rows),
            "validation": len(val_rows),
            "test": len(test_rows),
        },
        "train": _metric_block(train_rows, p_train, label_col),
        "validation": _metric_block(val_rows, p_val, label_col),
        "test": _metric_block(test_rows, p_test, label_col),
    }

    return TrainedModel(
        preprocessor=prep,
        model=best_model,
        feature_cols=feature_cols,
        label_col=label_col,
        row_filter_name=row_filter_name,
        metrics=metrics,
        search_selected=best_meta,
    )


def refit_binary_model_for_runtime(
    all_rows: List[Dict[str, Any]],
    *,
    evaluation_model: TrainedModel,
    strict: bool,
) -> TrainedModel:
    rows = _rows_for_model(
        all_rows,
        label_col=evaluation_model.label_col,
        filter_name=evaluation_model.row_filter_name,
    )
    if strict and not rows:
        raise SystemExit(
            f"Strict mode failed: no runtime-refit rows for "
            f"{evaluation_model.label_col}/{evaluation_model.row_filter_name}."
        )
    if strict and len(set(_label_int(r.get(evaluation_model.label_col)) for r in rows)) < 2:
        raise SystemExit(
            f"Strict mode failed: runtime-refit rows for "
            f"{evaluation_model.label_col}/{evaluation_model.row_filter_name} have one class."
        )
    if not rows:
        return evaluation_model

    prep = tbm.fit_preprocessor(train_rows=rows, feature_cols=evaluation_model.feature_cols)
    X, y = _dataset_xy(rows, prep, evaluation_model.label_col)
    if not X or len(X[0]) == 0:
        return evaluation_model

    l2 = float(evaluation_model.search_selected.get("l2", 0.1))
    lr = float(evaluation_model.search_selected.get("lr", 0.05))
    model = tbm.fit_logistic_regression(X, y, l2=l2, lr=lr)
    probs = tbm.predict_probabilities(model, X)
    metrics = dict(evaluation_model.metrics)
    metrics["runtime_refit"] = _metric_block(rows, probs, evaluation_model.label_col)
    metrics["runtime_refit"]["rows"] = len(rows)
    search_selected = dict(evaluation_model.search_selected)
    search_selected.update(
        {
            "runtime_refit_rows": len(rows),
            "runtime_refit_scope": "all_eligible_labeled_rows_after_hyperparameter_selection",
        }
    )
    return TrainedModel(
        preprocessor=prep,
        model=model,
        feature_cols=evaluation_model.feature_cols,
        label_col=evaluation_model.label_col,
        row_filter_name=evaluation_model.row_filter_name,
        metrics=metrics,
        search_selected=search_selected,
    )


def predict_rows(model: TrainedModel, rows: List[Dict[str, Any]]) -> List[float]:
    X = tbm.transform_rows(rows, model.preprocessor)
    return tbm.predict_probabilities(model.model, X)


def _trade_price(row: Dict[str, Any]) -> Optional[float]:
    for key in ["posted_limit", "limit_price", "decision_ask", "entry_ask", "t0_best_ask"]:
        p = _to_float(row.get(key))
        if p is None:
            continue
        if p <= 0.0 or p >= 1.0:
            continue
        return p
    return None


def _ev_if_filled(p_win_if_filled: float, price: float, stake: float) -> float:
    # Binary YES share expected value for stake dollars at price q:
    # profit(win)=stake*(1-q)/q ; profit(loss)=-stake
    win_profit = stake * ((1.0 - price) / price)
    return p_win_if_filled * win_profit - (1.0 - p_win_if_filled) * stake


def score_rows(
    rows: List[Dict[str, Any]],
    p_win: List[float],
    p_fill: List[float],
) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for row, pw, pf in zip(rows, p_win, p_fill):
        stake = _to_float(row.get("stake"))
        price = _trade_price(row)
        ev_if_filled = None
        ev_realized = None
        ev_per_stake = None
        if stake is not None and stake > 0 and price is not None:
            ev_if_filled = _ev_if_filled(pw, price, stake)
            ev_realized = pf * ev_if_filled
            ev_per_stake = ev_realized / stake
        out.append(
            {
                "mode": row.get("mode"),
                "bet_id": row.get("bet_id"),
                "session_date": row.get("session_date"),
                "signal_model_family": infer_signal_model_family(row),
                "split": row.get("split"),
                "stake": stake,
                "price_used": price,
                "target_filled": row.get("target_filled"),
                "target_win": row.get("target_win"),
                "target_profit": _to_float(row.get("target_profit")),
                "p_win_if_filled": pw,
                "p_fill": pf,
                "ev_if_filled": ev_if_filled,
                "ev_realized": ev_realized,
                "ev_per_stake": ev_per_stake,
            }
        )
    return out


def _max_drawdown(realized_profits: List[float]) -> float:
    peak = 0.0
    equity = 0.0
    mdd = 0.0
    for p in realized_profits:
        equity += p
        if equity > peak:
            peak = equity
        dd = peak - equity
        if dd > mdd:
            mdd = dd
    return mdd


def apply_policy(
    scored_rows: List[Dict[str, Any]],
    min_ev_per_stake: float,
    min_p_fill: float,
    max_per_day: int,
) -> List[Dict[str, Any]]:
    candidates = [
        r
        for r in scored_rows
        if r.get("ev_per_stake") is not None
        and r.get("ev_realized") is not None
        and float(r.get("ev_per_stake")) >= min_ev_per_stake
        and float(r.get("p_fill") or 0.0) >= min_p_fill
    ]
    if max_per_day <= 0:
        return sorted(candidates, key=lambda r: (str(r.get("session_date") or ""), -float(r.get("ev_realized") or 0.0), str(r.get("bet_id") or "")))

    by_date: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for row in candidates:
        by_date[str(row.get("session_date") or "")].append(row)

    selected: List[Dict[str, Any]] = []
    for d in sorted(by_date.keys()):
        day_rows = sorted(
            by_date[d],
            key=lambda r: (
                -float(r.get("ev_realized") or 0.0),
                -float(r.get("p_fill") or 0.0),
                str(r.get("bet_id") or ""),
            ),
        )
        selected.extend(day_rows[:max_per_day])

    selected.sort(key=lambda r: (str(r.get("session_date") or ""), str(r.get("bet_id") or "")))
    return selected


def policy_metrics(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    if not rows:
        return {
            "selected_trades": 0,
            "days_traded": 0,
            "realized_profit_sum": 0.0,
            "expected_profit_sum": 0.0,
            "total_stake": 0.0,
            "roi": None,
            "fill_rate": None,
            "win_rate_overall": None,
            "win_rate_filled_only": None,
            "avg_ev_per_stake": None,
            "max_drawdown": 0.0,
        }

    profits = [float(r.get("target_profit") or 0.0) for r in rows]
    evs = [float(r.get("ev_realized") or 0.0) for r in rows]
    stakes = [float(r.get("stake") or 0.0) for r in rows]
    fills = [_label_int(r.get("target_filled")) for r in rows if r.get("target_filled") is not None]
    wins_all = [_label_int(r.get("target_win")) for r in rows if r.get("target_win") is not None]
    filled_rows = [r for r in rows if _label_int(r.get("target_filled")) == 1 and r.get("target_win") is not None]
    wins_filled = [_label_int(r.get("target_win")) for r in filled_rows]

    total_stake = sum(stakes)
    return {
        "selected_trades": len(rows),
        "days_traded": len(set(str(r.get("session_date") or "") for r in rows)),
        "realized_profit_sum": sum(profits),
        "expected_profit_sum": sum(evs),
        "total_stake": total_stake,
        "roi": (sum(profits) / total_stake) if total_stake > 0 else None,
        "fill_rate": (sum(fills) / float(len(fills))) if fills else None,
        "win_rate_overall": (sum(wins_all) / float(len(wins_all))) if wins_all else None,
        "win_rate_filled_only": (sum(wins_filled) / float(len(wins_filled))) if wins_filled else None,
        "avg_ev_per_stake": (sum(float(r.get("ev_per_stake") or 0.0) for r in rows) / float(len(rows))),
        "max_drawdown": _max_drawdown(profits),
    }


def choose_best_policy(
    val_rows: List[Dict[str, Any]],
    ev_thresholds: List[float],
    pfill_thresholds: List[float],
    max_per_day_options: List[int],
    min_validation_trades: int,
) -> Tuple[Dict[str, Any], List[Dict[str, Any]], List[Dict[str, Any]]]:
    candidates: List[Dict[str, Any]] = []
    candidate_to_selected: Dict[Tuple[float, float, int], List[Dict[str, Any]]] = {}

    for ev_thr in ev_thresholds:
        for pf_thr in pfill_thresholds:
            for cap in max_per_day_options:
                selected = apply_policy(
                    val_rows,
                    min_ev_per_stake=ev_thr,
                    min_p_fill=pf_thr,
                    max_per_day=cap,
                )
                metrics = policy_metrics(selected)
                row = {
                    "min_ev_per_stake": ev_thr,
                    "min_p_fill": pf_thr,
                    "max_per_day": cap,
                    "metrics": metrics,
                }
                candidates.append(row)
                key = (float(ev_thr), float(pf_thr), int(cap))
                candidate_to_selected[key] = selected

    def rank_key(c: Dict[str, Any]) -> Tuple[float, float, float, float, int]:
        m = c["metrics"]
        return (
            float(m.get("expected_profit_sum") or 0.0),
            float(m.get("realized_profit_sum") or 0.0),
            float(m.get("roi") or -999.0),
            -float(m.get("max_drawdown") or 0.0),
            int(m.get("selected_trades") or 0),
        )

    eligible = [c for c in candidates if int(c["metrics"].get("selected_trades") or 0) >= min_validation_trades]
    positive_expected_eligible = [c for c in eligible if float(c["metrics"].get("expected_profit_sum") or 0.0) > 0.0]
    positive_expected_any = [c for c in candidates if float(c["metrics"].get("expected_profit_sum") or 0.0) > 0.0]

    if positive_expected_eligible:
        chosen_pool = positive_expected_eligible
        selection_stage = "positive_expected_with_min_trades"
    elif positive_expected_any:
        chosen_pool = positive_expected_any
        selection_stage = "positive_expected_any_trades"
    elif eligible:
        chosen_pool = eligible
        selection_stage = "max_expected_with_min_trades"
    else:
        chosen_pool = candidates
        selection_stage = "max_expected_any_trades"

    best_config = dict(sorted(chosen_pool, key=rank_key, reverse=True)[0])
    best_config["selection_stage"] = selection_stage
    best_selected = candidate_to_selected[
        (
            float(best_config["min_ev_per_stake"]),
            float(best_config["min_p_fill"]),
            int(best_config["max_per_day"]),
        )
    ]
    return best_config, best_selected, candidates


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
    if args.min_validation_trades < 0:
        raise SystemExit("--min-validation-trades must be >= 0")

    ev_thresholds = _parse_float_csv(args.ev_thresholds)
    pfill_thresholds = _parse_float_csv(args.pfill_thresholds, min_val=0.0, max_val=1.0)
    max_per_day_options = _parse_int_csv(args.max_per_day_options, min_val=0)

    args.output_root.mkdir(parents=True, exist_ok=True)

    rows = _filter_model_family_rows(_read_jsonl(args.table_path), args.model_family)
    if args.strict and not rows:
        raise SystemExit(f"Strict mode failed: no rows for model_family={args.model_family}.")
    manifest = _read_json(args.manifest_path)
    groups = manifest.get("column_groups", {})
    pre_signal = list(groups.get("pre_signal_features", []))
    post_signal = list(groups.get("post_signal_execution_features", []))
    if args.strict and not pre_signal:
        raise SystemExit("Strict mode failed: pre_signal_features missing in manifest.")

    win_features = _feature_list_for_win(pre_signal)
    runtime_fill_features = _feature_list_for_runtime_fill(pre_signal)
    strict_fill_features = _feature_list_for_fill(pre_signal, post_signal)

    win_model = train_binary_model(
        all_rows=rows,
        label_col="target_win",
        feature_cols=win_features,
        row_filter_name="filled_only",
        strict=args.strict,
    )
    runtime_fill_model = train_binary_model(
        all_rows=rows,
        label_col="target_filled",
        feature_cols=runtime_fill_features,
        row_filter_name="all_labeled",
        strict=args.strict,
    )
    strict_fill_model = train_binary_model(
        all_rows=rows,
        label_col="target_filled",
        feature_cols=strict_fill_features,
        row_filter_name="all_labeled",
        strict=args.strict,
    )
    artifact_win_model = win_model
    artifact_runtime_fill_model = runtime_fill_model
    artifact_strict_fill_model = strict_fill_model
    if args.artifact_purpose == "runtime-refit":
        artifact_win_model = refit_binary_model_for_runtime(
            rows,
            evaluation_model=win_model,
            strict=args.strict,
        )
        artifact_runtime_fill_model = refit_binary_model_for_runtime(
            rows,
            evaluation_model=runtime_fill_model,
            strict=args.strict,
        )
        artifact_strict_fill_model = refit_binary_model_for_runtime(
            rows,
            evaluation_model=strict_fill_model,
            strict=args.strict,
        )

    policy_rows = _filter_policy_rows(rows, policy_mode=args.policy_mode)
    if args.strict and not policy_rows:
        raise SystemExit("Strict mode failed: no policy rows after filtering.")

    p_win = predict_rows(win_model, policy_rows)
    p_fill = predict_rows(runtime_fill_model, policy_rows)
    scored_rows = score_rows(policy_rows, p_win=p_win, p_fill=p_fill)

    by_split: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for row in scored_rows:
        by_split[str(row.get("split") or "train")].append(row)

    val_rows = sorted(by_split.get("validation", []), key=lambda r: (str(r.get("session_date") or ""), str(r.get("bet_id") or "")))
    test_rows = sorted(by_split.get("test", []), key=lambda r: (str(r.get("session_date") or ""), str(r.get("bet_id") or "")))
    train_rows = sorted(by_split.get("train", []), key=lambda r: (str(r.get("session_date") or ""), str(r.get("bet_id") or "")))

    baseline_val = policy_metrics(val_rows)
    baseline_test = policy_metrics(test_rows)

    best_cfg, val_selected, val_grid = choose_best_policy(
        val_rows=val_rows,
        ev_thresholds=ev_thresholds,
        pfill_thresholds=pfill_thresholds,
        max_per_day_options=max_per_day_options,
        min_validation_trades=args.min_validation_trades,
    )

    test_selected = apply_policy(
        test_rows,
        min_ev_per_stake=float(best_cfg["min_ev_per_stake"]),
        min_p_fill=float(best_cfg["min_p_fill"]),
        max_per_day=int(best_cfg["max_per_day"]),
    )

    train_selected = apply_policy(
        train_rows,
        min_ev_per_stake=float(best_cfg["min_ev_per_stake"]),
        min_p_fill=float(best_cfg["min_p_fill"]),
        max_per_day=int(best_cfg["max_per_day"]),
    )

    # Attach selection flags for convenience.
    selected_keys_val = {(r["bet_id"], r["split"]) for r in val_selected}
    selected_keys_test = {(r["bet_id"], r["split"]) for r in test_selected}
    selected_keys_train = {(r["bet_id"], r["split"]) for r in train_selected}
    for row in scored_rows:
        key = (str(row.get("bet_id") or ""), str(row.get("split") or ""))
        row["selected_best_policy"] = key in selected_keys_train or key in selected_keys_val or key in selected_keys_test
        row["selected_best_policy_split_only"] = key in selected_keys_val or key in selected_keys_test

    scored_rows.sort(key=lambda r: (str(r.get("session_date") or ""), str(r.get("split") or ""), str(r.get("bet_id") or "")))
    val_grid_sorted = sorted(
        val_grid,
        key=lambda c: (
            float(c["metrics"]["expected_profit_sum"] or 0.0),
            float(c["metrics"]["realized_profit_sum"] or 0.0),
            float(c["metrics"]["roi"] or -999.0),
            -float(c["metrics"]["max_drawdown"] or 0.0),
            int(c["metrics"]["selected_trades"] or 0),
        ),
        reverse=True,
    )

    report = {
        "generated_at_utc": _now_iso(),
        "config": {
            "table_path": str(args.table_path),
            "manifest_path": str(args.manifest_path),
            "output_root": str(args.output_root),
            "policy_mode": args.policy_mode,
            "model_family": args.model_family,
            "artifact_purpose": args.artifact_purpose,
            "ev_thresholds": ev_thresholds,
            "pfill_thresholds": pfill_thresholds,
            "max_per_day_options": max_per_day_options,
            "min_validation_trades": args.min_validation_trades,
            "strict": bool(args.strict),
        },
        "models": {
            "signal_win_if_filled": {
                "label_col": win_model.label_col,
                "row_filter": win_model.row_filter_name,
                "feature_count": len(win_model.preprocessor.feature_names),
                "feature_cols_used": win_model.feature_cols,
                "search_selected": win_model.search_selected,
                "metrics": win_model.metrics,
                "top_coefficients": tbm._top_coefficients(win_model.model, win_model.preprocessor),
            },
            "execution_fill_strict": {
                "label_col": strict_fill_model.label_col,
                "row_filter": strict_fill_model.row_filter_name,
                "feature_count": len(strict_fill_model.preprocessor.feature_names),
                "feature_cols_used": strict_fill_model.feature_cols,
                "search_selected": strict_fill_model.search_selected,
                "metrics": strict_fill_model.metrics,
                "top_coefficients": tbm._top_coefficients(strict_fill_model.model, strict_fill_model.preprocessor),
            },
            "execution_fill_runtime": {
                "label_col": runtime_fill_model.label_col,
                "row_filter": runtime_fill_model.row_filter_name,
                "feature_count": len(runtime_fill_model.preprocessor.feature_names),
                "feature_cols_used": runtime_fill_model.feature_cols,
                "search_selected": runtime_fill_model.search_selected,
                "metrics": runtime_fill_model.metrics,
                "top_coefficients": tbm._top_coefficients(runtime_fill_model.model, runtime_fill_model.preprocessor),
            },
        },
        "policy_baseline_no_filter": {
            "train": policy_metrics(train_rows),
            "validation": baseline_val,
            "test": baseline_test,
        },
        "policy_selection": {
            "best_validation_config": best_cfg,
            "best_validation_metrics": policy_metrics(val_selected),
            "best_train_metrics": policy_metrics(train_selected),
            "best_test_metrics": policy_metrics(test_selected),
            "top_validation_grid": val_grid_sorted[:25],
        },
        "notes": {
            "ev_definition": "ev_realized = p_fill_runtime * ev_if_filled; ev_if_filled uses binary YES-share payout math from price and stake.",
            "runtime_feature_policy": "Runtime EV models use reliable decision-time pre-signal features only; post-signal book horizons, token IDs, provenance timestamps, stadium names/IDs, and Kelly sizing diagnostics are excluded.",
            "strict_fill_feature_policy": "Offline strict execution model may include post-signal non-sim_* book horizons for diagnostics, but live runtime must not load it.",
            "split_protocol": "Threshold/cap chosen on validation only; final performance reported on test.",
            "runtime_refit_protocol": "When artifact_purpose=runtime-refit, model/policy selection remains split-based, but exported model weights are refit on all eligible labeled rows using selected hyperparameters.",
        },
        "best_config": best_cfg,
    }

    report_path = args.output_root / "ev_policy_report.json"
    scored_path = args.output_root / "ev_scored_rows.jsonl"
    val_sel_path = args.output_root / "ev_policy_validation_trades.jsonl"
    test_sel_path = args.output_root / "ev_policy_test_trades.jsonl"
    model_win_path = args.output_root / "ev_signal_win_if_filled_model.json"
    model_fill_runtime_path = args.output_root / "ev_execution_fill_runtime_model.json"
    model_fill_strict_path = args.output_root / "ev_execution_fill_strict_model.json"

    # Active #16 v2 (2026-05-17): stamp build-time lineage on every
    # artifact this builder produces. The two runtime-loaded JSONs
    # (ev_signal_win_if_filled_model + ev_execution_fill_runtime_model)
    # are the highest-value targets -- when fast Wilson-UB demote
    # fires on the EV policy lever, the operator's first question is
    # "which fit + which git_sha was in production?" Lineage answers
    # that. Fail-open: stamp failure must not block the artifact
    # writes.
    try:
        from scripts.analysis.artifact_lineage import compute_lineage as _compute_lineage
    except ImportError:
        try:
            from artifact_lineage import compute_lineage as _compute_lineage  # type: ignore[no-redef]
        except ImportError:
            _compute_lineage = None  # type: ignore[assignment]
    _ev_lineage: Optional[Dict[str, Any]] = None
    if _compute_lineage is not None:
        try:
            from pathlib import Path as _P
            _project_dir = _P(__file__).resolve().parents[2]
            _ev_lineage = _compute_lineage(
                builder_path=__file__,
                input_paths=[args.table_path, args.manifest_path],
                project_root=_project_dir,
                extra={
                    "cli_args_summary": {
                        "model_family": args.model_family,
                        "artifact_purpose": args.artifact_purpose,
                        "table_path": str(args.table_path),
                        "manifest_path": str(args.manifest_path),
                        "output_root": str(args.output_root),
                    },
                },
            )
        except Exception as _lineage_exc:  # noqa: BLE001
            LOGGER.warning(
                "EV-policy lineage stamp failed: %r (artifacts still written)",
                _lineage_exc,
            )
    if _ev_lineage is not None:
        report["lineage"] = _ev_lineage

    _write_json(report_path, report)
    _write_jsonl(scored_path, scored_rows)
    _write_jsonl(val_sel_path, val_selected)
    _write_jsonl(test_sel_path, test_selected)
    def _with_lineage(payload: Dict[str, Any]) -> Dict[str, Any]:
        # Mutate-then-return so the lineage block sits on the same
        # artifact dict the runtime will read. Fail-open: no lineage
        # available simply leaves the artifact unstamped.
        if _ev_lineage is not None:
            payload["lineage"] = _ev_lineage
        return payload

    _write_json(
        model_win_path,
        _with_lineage(_model_artifact_payload(
            trained=artifact_win_model,
            artifact_role="ev_policy_signal_win_if_filled",
            runtime_safe=True,
            feature_policy="decision_time_runtime_reliable",
            model_family=args.model_family,
            artifact_purpose=args.artifact_purpose,
            evaluation_metrics=win_model.metrics,
        )),
    )
    _write_json(
        model_fill_runtime_path,
        _with_lineage(_model_artifact_payload(
            trained=artifact_runtime_fill_model,
            artifact_role="ev_policy_execution_fill_runtime",
            runtime_safe=True,
            feature_policy="decision_time_runtime_reliable",
            model_family=args.model_family,
            artifact_purpose=args.artifact_purpose,
            evaluation_metrics=runtime_fill_model.metrics,
        )),
    )
    _write_json(
        model_fill_strict_path,
        _with_lineage(_model_artifact_payload(
            trained=artifact_strict_fill_model,
            artifact_role="ev_policy_execution_fill_strict",
            runtime_safe=False,
            feature_policy="post_signal_execution_analysis",
            model_family=args.model_family,
            artifact_purpose=args.artifact_purpose,
            evaluation_metrics=strict_fill_model.metrics,
        )),
    )

    LOGGER.info("Wrote %s", report_path)
    LOGGER.info("Wrote %s", scored_path)
    LOGGER.info("Wrote %s", val_sel_path)
    LOGGER.info("Wrote %s", test_sel_path)
    LOGGER.info("Wrote %s", model_win_path)
    LOGGER.info("Wrote %s", model_fill_runtime_path)
    LOGGER.info("Wrote %s", model_fill_strict_path)


if __name__ == "__main__":
    main()
