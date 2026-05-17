#!/usr/bin/env python3
"""
Fit fair-value probability calibration on unified signal data.

Uses settled rows from signals_master.jsonl and won_counterfactual labels.
Fits two calibration models:
  1) Platt scaling (logistic on logit(raw_prob))
  2) Isotonic mapping (PAV)

Outputs:
  data/analysis_output/calibration/
    signal_win_calibration.json
    signal_win_calibration_report.json
    signal_win_calibration_predictions.jsonl
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple


PROJECT_DIR = Path(__file__).resolve().parents[2]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from scripts.trading.model_families import (
    KNOWN_MODEL_FAMILIES,
    SCORE_EVENT_TRANSITION,
    infer_signal_model_family,
)

DEFAULT_INPUT_PATH = PROJECT_DIR / "data" / "analysis_output" / "unified_signals" / "signals_master.jsonl"
DEFAULT_OUTPUT_ROOT = PROJECT_DIR / "data" / "analysis_output" / "calibration"
DEFAULT_OUTPUT_STEM = "signal_win_calibration"

# UNDER calibrator (Phase A2, 2026-05-16). `--side under` fits a
# separate calibration artifact with flipped labels (1 - over_label)
# and flipped raw probs (1 - over_raw). Mathematically a perfect
# calibrator on Over would invert exactly, but Platt + isotonic fits
# are NOT symmetric under flip: tail asymmetry (e.g. games ending 0-0
# in regulation are a massive UNDER win that Over calibration never
# treats as a positive outcome) means a separately-fit UNDER curve
# can diverge meaningfully from `1 - over_calibrator(p_over)`. Keeps
# a separate selection_history so the UNDER stability gate is not
# contaminated by Over's method flips.
DEFAULT_OUTPUT_STEM_UNDER = "signal_win_calibration_under"
DEFAULT_SELECTION_HISTORY_PATH_UNDER = (
    PROJECT_DIR / "data" / "analysis_output" / "calibration"
    / "selection_history_under.jsonl"
)

# When validation is degenerate (single class), best-validation-logloss can
# pick "raw" (identity) by default because there are no negatives or no
# positives to penalize an overconfident model. In that case, prefer
# Platt/isotonic if they are at least this much better-calibrated on the
# training set (10-bin ECE). 0.05 = 5pp absolute calibration-error gap.
DEFAULT_IDENTITY_REJECTION_TRAIN_ECE_DELTA = 0.05

# Method-stability gate (shipped 2026-05-14). Daily refreshes were picking
# different calibrators day-over-day on small validation samples
# (platt <-> isotonic flip-flop), making the runtime calibrator itself a
# source of FV instability. The gate reads the trailing-N-day selection
# history and, when today's pick differs from the modal pick of the last
# N days, overrides to the modal -- meaning we only flip methods after
# the new pick has won several days in a row. Disable with
# `--no-stability-gate` to backfill or debug.
DEFAULT_SELECTION_HISTORY_PATH = (
    PROJECT_DIR / "data" / "analysis_output" / "calibration"
    / "selection_history.jsonl"
)
DEFAULT_STABILITY_WINDOW = 7         # trailing distinct-date observations
DEFAULT_STABILITY_MIN_HISTORY = 5    # need >= N distinct dates before applying

# Input-drift audit (shipped 2026-05-16). When the concept_drift report
# shows >= 2 CONTINUOUS features (metric=="PSI") at verdict=="major"
# (PSI >= 0.25), the calibrator's training-distribution is materially
# different from inference. The runtime calibrator retrains daily anyway,
# but flagging the situation tells operators today's selected method
# was chosen on drift-shifted data -- so the stability gate they trust
# is itself standing on shifting ground.
#
# Categorical TVD (stadium_id) doesn't count: stadium distribution
# shifts are a real signal but they don't directly imply calibration
# error the way continuous-feature distribution shifts do.
DEFAULT_CONCEPT_DRIFT_REPORT_PATH = (
    PROJECT_DIR / "data" / "analysis_output" / "concept_drift"
    / "concept_drift_report.json"
)
INPUT_DRIFT_TRIGGER_MIN_MAJOR_FEATURES = 2
INPUT_DRIFT_TRIGGER_PSI_THRESHOLD = 0.25

LOGGER = logging.getLogger("calibrate_signal_probabilities")


@dataclass
class Sample:
    bet_id: str
    session_date: str
    mode: str
    raw_prob: float
    raw_prob_source: str
    label: int
    model_family: str
    decision_ask: Optional[float]
    line: Optional[str]
    inning: Optional[int]
    split: str = "train"


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Calibrate signal fair_value probabilities from unified signal rows.")
    p.add_argument("--input-path", type=Path, default=DEFAULT_INPUT_PATH)
    p.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    p.add_argument(
        "--output-stem",
        type=str,
        default="",
        help=(
            "Output basename (no extension). Defaults to "
            f"'{DEFAULT_OUTPUT_STEM}' when --side over, "
            f"'{DEFAULT_OUTPUT_STEM_UNDER}' when --side under."
        ),
    )
    p.add_argument(
        "--side",
        choices=["over", "under"],
        default="over",
        help=(
            "Which side to calibrate. 'over' is the default; matches "
            "the legacy single-side behavior. 'under' flips labels and "
            "raw probabilities, fits a separately-trained UNDER "
            "calibrator, writes to a separate artifact path, and "
            "maintains a separate stability-gate selection history. "
            "Phase A2 foundation: UNDER calibration is offline / "
            "shadow only until Phase B/C wire it into the live engine."
        ),
    )
    p.add_argument("--mode", choices=["live", "paper", "both"], default="both")
    p.add_argument(
        "--input-kind",
        choices=["auto", "signals_master", "candidate_universe", "calibration_opportunity_training"],
        default="auto",
        help=(
            "Label schema to read. 'auto' detects: calibration_opportunity_training "
            "when label_final_available is present, candidate_universe when "
            "target_counterfactual_win/label_available is present, otherwise "
            "signals_master."
        ),
    )
    p.add_argument(
        "--family-mode",
        choices=["separate", "pooled"],
        default="separate",
        help="Fit one curve per signal_model_family or one pooled curve (default: separate).",
    )
    p.add_argument(
        "--model-family",
        default="all",
        help="Optional family filter: all, score_event_transition, no_score_drift.",
    )
    p.add_argument(
        "--default-family",
        default=SCORE_EVENT_TRANSITION,
        help=f"Family used by runtime callers that omit family (default: {SCORE_EVENT_TRANSITION}).",
    )
    p.add_argument("--min-date", type=str, default="")
    p.add_argument("--max-date", type=str, default="")
    p.add_argument("--val-frac", type=float, default=0.15)
    p.add_argument("--test-frac", type=float, default=0.15)
    p.add_argument(
        "--artifact-purpose",
        choices=["evaluation", "runtime-refit"],
        default="evaluation",
        help=(
            "evaluation keeps train/validation/test-fitted parameters for "
            "leakage-aware reporting. runtime-refit selects the method via "
            "the same evaluation split, then refits exported calibration "
            "parameters on all eligible labeled rows."
        ),
    )
    p.add_argument(
        "--identity-rejection-train-ece-delta",
        type=float,
        default=DEFAULT_IDENTITY_REJECTION_TRAIN_ECE_DELTA,
        help=(
            "When validation logloss picks identity, override to the best "
            "challenger if its train ECE is at least this much lower than "
            "raw's train ECE. 0 disables the guard."
        ),
    )
    p.add_argument("--strict", action="store_true")
    p.add_argument("--verbose", action="store_true")
    p.add_argument(
        "--selection-history-path",
        type=Path,
        default=None,
        help=(
            "Append-only JSONL of per-refresh per-family pre-override and "
            "final selections. Drives the method-stability gate. "
            f"Defaults to '{DEFAULT_SELECTION_HISTORY_PATH.name}' when "
            f"--side over, '{DEFAULT_SELECTION_HISTORY_PATH_UNDER.name}' "
            "when --side under, so the two sides' gates stay independent."
        ),
    )
    p.add_argument(
        "--stability-window",
        type=int,
        default=DEFAULT_STABILITY_WINDOW,
        help=(
            "Trailing distinct-date window used to compute the modal "
            "calibration method. Today's pick is overridden to the modal "
            "when they differ."
        ),
    )
    p.add_argument(
        "--stability-min-history",
        type=int,
        default=DEFAULT_STABILITY_MIN_HISTORY,
        help=(
            "Minimum number of distinct prior dates needed before the "
            "stability gate is allowed to override. Below this, today's "
            "pick passes through unchanged."
        ),
    )
    p.add_argument(
        "--no-stability-gate",
        dest="stability_gate_enabled",
        action="store_false",
        default=True,
        help="Disable the stability gate (no override + no history append).",
    )
    p.add_argument(
        "--concept-drift-report-path",
        type=Path,
        default=DEFAULT_CONCEPT_DRIFT_REPORT_PATH,
        help=(
            "Concept-drift report consulted to flag today's calibration "
            "selection_audit with input_drift_triggered when >= "
            f"{INPUT_DRIFT_TRIGGER_MIN_MAJOR_FEATURES} continuous features "
            f"show PSI >= {INPUT_DRIFT_TRIGGER_PSI_THRESHOLD}. Missing/unreadable "
            "report falls back to input_drift_status='report_missing' / "
            "'report_unreadable' without failing the calibration."
        ),
    )
    return p.parse_args(argv)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


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


def _safe_float(v: Any) -> Optional[float]:
    try:
        if v is None or v == "":
            return None
        return float(v)
    except Exception:
        return None


def _safe_int(v: Any) -> Optional[int]:
    try:
        if v is None or v == "":
            return None
        return int(v)
    except Exception:
        return None


def _safe_bool_int(v: Any) -> Optional[int]:
    if v is None:
        return None
    if isinstance(v, bool):
        return 1 if v else 0
    if isinstance(v, (int, float)):
        return 1 if int(v) == 1 else 0
    s = str(v).strip().lower()
    if s in {"1", "true", "yes", "y"}:
        return 1
    if s in {"0", "false", "no", "n"}:
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


def _session_date(row: Dict[str, Any]) -> str:
    d = str(row.get("session_date") or "")
    if d:
        return d
    placed_at = str(row.get("placed_at") or "")
    if len(placed_at) >= 10:
        return placed_at[:10]
    bet_id = str(row.get("bet_id") or "")
    if len(bet_id) >= 10 and bet_id[4] == "-" and bet_id[7] == "-":
        return bet_id[:10]
    return ""


def _input_kind_for_row(row: Dict[str, Any], input_kind: str) -> str:
    if input_kind != "auto":
        return input_kind
    # Order matters: calibration_opportunity_training rows can also carry
    # legacy fields, so check its discriminating field first.
    if "label_final_available" in row or "target_over_win" in row:
        return "calibration_opportunity_training"
    if "target_counterfactual_win" in row or "label_available" in row:
        return "candidate_universe"
    return "signals_master"


def _label_for_row(row: Dict[str, Any], input_kind: str) -> Tuple[Optional[int], str]:
    kind = _input_kind_for_row(row, input_kind)
    if kind == "calibration_opportunity_training":
        if not bool(row.get("label_final_available")):
            return None, "label_final_unavailable"
        label = _safe_bool_int(row.get("target_over_win"))
        if label is None:
            return None, "missing_target_over_win"
        return label, "ok"

    if kind == "candidate_universe":
        if not bool(row.get("label_available")):
            return None, "label_unavailable"
        label = _safe_bool_int(row.get("target_counterfactual_win"))
        if label is None:
            return None, "missing_target_counterfactual_win"
        return label, "ok"

    settled = bool(row.get("settled")) if row.get("settled") is not None else False
    if not settled:
        return None, "unsettled"
    label = _safe_bool_int(row.get("won_counterfactual"))
    if label is None:
        return None, "missing_label"
    return label, "ok"


def _family_filter_passes(model_family: str, requested: str) -> bool:
    requested = str(requested or "all").strip()
    if requested in {"", "all"}:
        return True
    return model_family == requested


def _clip_prob(p: float) -> float:
    return min(max(float(p), 1e-8), 1.0 - 1e-8)


def _stable_sigmoid(x: float) -> float:
    if x >= 0:
        z = math.exp(-x)
        return 1.0 / (1.0 + z)
    z = math.exp(x)
    return z / (1.0 + z)


def _logit(p: float) -> float:
    cp = _clip_prob(p)
    return math.log(cp / (1.0 - cp))


def _logloss(y: Sequence[int], p: Sequence[float]) -> Optional[float]:
    if not y:
        return None
    eps = 1e-12
    total = 0.0
    for yi, pi in zip(y, p):
        pc = min(max(float(pi), eps), 1.0 - eps)
        total += -(yi * math.log(pc) + (1 - yi) * math.log(1.0 - pc))
    return total / float(len(y))


def _brier(y: Sequence[int], p: Sequence[float]) -> Optional[float]:
    if not y:
        return None
    return sum((yi - pi) ** 2 for yi, pi in zip(y, p)) / float(len(y))


def _ece(y: Sequence[int], p: Sequence[float], bins: int = 10) -> Optional[float]:
    if not y:
        return None
    n = len(y)
    total = 0.0
    for b in range(bins):
        lo = b / bins
        hi = (b + 1) / bins
        if b == bins - 1:
            idx = [i for i, pi in enumerate(p) if lo <= pi <= hi]
        else:
            idx = [i for i, pi in enumerate(p) if lo <= pi < hi]
        if not idx:
            continue
        avg_p = sum(p[i] for i in idx) / len(idx)
        avg_y = sum(y[i] for i in idx) / len(idx)
        total += abs(avg_p - avg_y) * (len(idx) / n)
    return total


def _reliability_bins(y: Sequence[int], p: Sequence[float], bins: int = 10) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for b in range(bins):
        lo = b / bins
        hi = (b + 1) / bins
        if b == bins - 1:
            idx = [i for i, pi in enumerate(p) if lo <= pi <= hi]
        else:
            idx = [i for i, pi in enumerate(p) if lo <= pi < hi]
        if not idx:
            out.append({"bin_start": lo, "bin_end": hi, "count": 0, "avg_pred": None, "hit_rate": None})
            continue
        out.append(
            {
                "bin_start": lo,
                "bin_end": hi,
                "count": len(idx),
                "avg_pred": sum(p[i] for i in idx) / len(idx),
                "hit_rate": sum(y[i] for i in idx) / len(idx),
            }
        )
    return out


def _slice_overconfidence(
    y: Sequence[int],
    raw_p: Sequence[float],
    cal_p: Sequence[float],
    thresholds: Sequence[float] = (0.85, 0.90, 0.95),
) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for t in thresholds:
        idx = [i for i, rp in enumerate(raw_p) if rp >= t]
        if not idx:
            out.append(
                {
                    "threshold_raw": t,
                    "count": 0,
                    "avg_raw_prob": None,
                    "avg_cal_prob": None,
                    "hit_rate": None,
                    "raw_gap": None,
                    "cal_gap": None,
                }
            )
            continue
        avg_raw = sum(raw_p[i] for i in idx) / len(idx)
        avg_cal = sum(cal_p[i] for i in idx) / len(idx)
        hit = sum(y[i] for i in idx) / len(idx)
        out.append(
            {
                "threshold_raw": t,
                "count": len(idx),
                "avg_raw_prob": avg_raw,
                "avg_cal_prob": avg_cal,
                "hit_rate": hit,
                "raw_gap": avg_raw - hit,
                "cal_gap": avg_cal - hit,
            }
        )
    return out


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


def _split_dates(dates: List[str], val_frac: float, test_frac: float) -> Dict[str, List[str]]:
    uniq = sorted(set(d for d in dates if d))
    train_n, val_n, test_n = _allocate_split_counts(len(uniq), val_frac, test_frac)
    return {
        "train": uniq[:train_n],
        "validation": uniq[train_n : train_n + val_n],
        "test": uniq[train_n + val_n : train_n + val_n + test_n],
    }


def _fit_platt(
    probs: Sequence[float],
    labels: Sequence[int],
    l2: float = 1e-3,
    lr: float = 0.05,
    max_iter: int = 4000,
    tol: float = 1e-9,
) -> Dict[str, float]:
    if not probs:
        return {"a": 1.0, "b": 0.0}
    xs = [_logit(p) for p in probs]
    pos = sum(labels) / float(len(labels))
    pos = min(max(pos, 1e-6), 1.0 - 1e-6)
    a = 1.0
    b = math.log(pos / (1.0 - pos))
    prev_loss: Optional[float] = None

    for it in range(max_iter):
        grad_a = 0.0
        grad_b = 0.0
        loss = 0.0
        n = len(xs)
        for x, y in zip(xs, labels):
            z = a * x + b
            p = _stable_sigmoid(z)
            pc = min(max(p, 1e-12), 1.0 - 1e-12)
            loss += -(y * math.log(pc) + (1 - y) * math.log(1.0 - pc))
            diff = p - y
            grad_a += diff * x
            grad_b += diff
        loss = (loss / n) + 0.5 * l2 * (a * a)
        grad_a = (grad_a / n) + l2 * a
        grad_b = grad_b / n
        step = lr / math.sqrt(1.0 + (0.01 * it))
        a -= step * grad_a
        b -= step * grad_b
        if prev_loss is not None and abs(prev_loss - loss) < tol:
            break
        prev_loss = loss
    return {"a": float(a), "b": float(b)}


def _predict_platt(probs: Sequence[float], params: Dict[str, float]) -> List[float]:
    a = float(params.get("a", 1.0))
    b = float(params.get("b", 0.0))
    return [_clip_prob(_stable_sigmoid(a * _logit(p) + b)) for p in probs]


def _fit_isotonic(probs: Sequence[float], labels: Sequence[int]) -> Dict[str, List[float]]:
    if not probs:
        return {"x": [0.0, 1.0], "y": [0.0, 1.0]}

    pairs = sorted(zip(probs, labels), key=lambda x: x[0])
    blocks: List[Dict[str, float]] = [
        {"w": 1.0, "sum_y": float(y), "sum_x": float(p)}
        for p, y in pairs
    ]

    i = 0
    while i < len(blocks) - 1:
        mean_i = blocks[i]["sum_y"] / blocks[i]["w"]
        mean_j = blocks[i + 1]["sum_y"] / blocks[i + 1]["w"]
        if mean_i > mean_j:
            blocks[i]["w"] += blocks[i + 1]["w"]
            blocks[i]["sum_y"] += blocks[i + 1]["sum_y"]
            blocks[i]["sum_x"] += blocks[i + 1]["sum_x"]
            del blocks[i + 1]
            if i > 0:
                i -= 1
        else:
            i += 1

    xs: List[float] = []
    ys: List[float] = []
    ws: List[float] = []
    for b in blocks:
        w = float(b["w"])
        x = float(_clip_prob(b["sum_x"] / w))
        y = float(_clip_prob(b["sum_y"] / w))
        if xs and abs(x - xs[-1]) < 1e-12:
            prev_w = ws[-1]
            total_w = prev_w + w
            xs[-1] = ((xs[-1] * prev_w) + (x * w)) / total_w
            ys[-1] = ((ys[-1] * prev_w) + (y * w)) / total_w
            ws[-1] = total_w
            continue
        xs.append(x)
        ys.append(y)
        ws.append(w)

    if len(xs) == 1:
        x = xs[0]
        y = ys[0]
        xs = [0.0, x, 1.0]
        ys = [y, y, y]
    if xs[0] > 0.0:
        xs.insert(0, 0.0)
        ys.insert(0, ys[0])
    if xs[-1] < 1.0:
        xs.append(1.0)
        ys.append(ys[-1])
    return {"x": xs, "y": ys}


def _predict_isotonic(probs: Sequence[float], params: Dict[str, List[float]]) -> List[float]:
    xs = [float(x) for x in params.get("x", [])]
    ys = [float(y) for y in params.get("y", [])]
    if len(xs) < 2 or len(xs) != len(ys):
        return [_clip_prob(p) for p in probs]

    out: List[float] = []
    for p in probs:
        cp = _clip_prob(p)
        if cp <= xs[0]:
            out.append(_clip_prob(ys[0]))
            continue
        if cp >= xs[-1]:
            out.append(_clip_prob(ys[-1]))
            continue
        j = 1
        while j < len(xs) and cp > xs[j]:
            j += 1
        x0, x1 = xs[j - 1], xs[j]
        y0, y1 = ys[j - 1], ys[j]
        if x1 <= x0:
            out.append(_clip_prob(y0))
            continue
        t = (cp - x0) / (x1 - x0)
        out.append(_clip_prob(y0 + (y1 - y0) * t))
    return out


def _metrics_bundle(
    labels: Sequence[int],
    raw_probs: Sequence[float],
    method_probs: Sequence[float],
) -> Dict[str, Any]:
    return {
        "rows": len(labels),
        "positive_rate": (sum(labels) / float(len(labels))) if labels else None,
        "logloss": _logloss(labels, method_probs),
        "brier": _brier(labels, method_probs),
        "ece_10": _ece(labels, method_probs, bins=10),
        "reliability_bins_10": _reliability_bins(labels, method_probs, bins=10),
        "high_fv_slices": _slice_overconfidence(labels, raw_probs, method_probs),
    }


def _select_best_method(
    method_eval: Dict[str, Dict[str, Any]],
    *,
    identity_rejection_train_ece_delta: float = DEFAULT_IDENTITY_REJECTION_TRAIN_ECE_DELTA,
) -> Tuple[str, Dict[str, Any]]:
    """Select the best calibration method, with an identity-rejection guard.

    Primary: best validation logloss across raw/platt/isotonic.

    Guard: when the primary picks "raw" (identity), check whether
    Platt/isotonic is materially better-calibrated on the training set
    (10-bin ECE delta >= ``identity_rejection_train_ece_delta``). Validation
    can pick "raw" by default when its split is degenerate (e.g. all-positive)
    -- identity returns the over-confident raw FV, which scores low logloss
    against an all-positive label set even though it is poorly calibrated in
    general. Train ECE is a more honest tiebreaker in that case.

    Returns ``(selected_name, audit_dict)`` where ``audit_dict`` records the
    primary winner and whether the guard fired so the artifact stays
    explainable.
    """
    primary_name = "raw"
    primary_score: Optional[float] = None
    for name in ("raw", "platt", "isotonic"):
        metrics = method_eval.get(name, {}).get("validation", {})
        score = metrics.get("logloss")
        if score is None:
            continue
        if primary_score is None or score < primary_score:
            primary_score = score
            primary_name = name

    audit: Dict[str, Any] = {
        "primary_winner": primary_name,
        "primary_validation_logloss": primary_score,
        "identity_rejection_threshold": float(identity_rejection_train_ece_delta),
        "identity_rejection_applied": False,
    }

    selected = primary_name
    if primary_name == "raw":
        raw_train = method_eval.get("raw", {}).get("train", {}) or {}
        raw_train_ece = raw_train.get("ece_10")
        challenger: Optional[str] = None
        challenger_ece: Optional[float] = None
        for name in ("platt", "isotonic"):
            ece = (method_eval.get(name, {}).get("train", {}) or {}).get("ece_10")
            if ece is None:
                continue
            if challenger_ece is None or ece < challenger_ece:
                challenger_ece = ece
                challenger = name
        audit["raw_train_ece"] = raw_train_ece
        audit["challenger"] = challenger
        audit["challenger_train_ece"] = challenger_ece
        if (
            challenger is not None
            and raw_train_ece is not None
            and challenger_ece is not None
            and (raw_train_ece - challenger_ece) >= identity_rejection_train_ece_delta
        ):
            selected = challenger
            audit["identity_rejection_applied"] = True
            audit["identity_rejection_train_ece_gap"] = raw_train_ece - challenger_ece

    audit["selected"] = "identity" if selected == "raw" else selected
    return ("identity" if selected == "raw" else selected), audit


# ---------------------------------------------------------------------------
# Method-stability gate
# ---------------------------------------------------------------------------

def _load_selection_history(path: Path) -> List[Dict[str, Any]]:
    """Read the selection-history JSONL. Returns rows in file order."""
    if not path.exists():
        return []
    out: List[Dict[str, Any]] = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        return []
    return out


def _history_row_date(row: Dict[str, Any]) -> str:
    """Pick a stable date key for the history row.

    Prefer the explicit `data_max_date` (data-window upper bound), since
    that's the operationally meaningful day this calibration represents.
    Fall back to the YYYY-MM-DD prefix of `generated_at_utc`.
    """
    d = row.get("data_max_date")
    if d:
        return str(d)[:10]
    g = row.get("generated_at_utc") or ""
    return str(g)[:10] if g else ""


def _trailing_family_history(
    history_rows: List[Dict[str, Any]],
    family: str,
    *,
    window: int,
    exclude_date: Optional[str] = None,
) -> List[str]:
    """Return the last `window` distinct-date pre-override selections for
    `family`, oldest first. Same-date rows are deduped to the latest entry.

    `exclude_date` skips an effective date (typically today's max_date) so
    a re-run on the same day doesn't compare against its own earlier write.
    """
    by_date: "Dict[str, str]" = {}
    for row in history_rows:
        d = _history_row_date(row)
        if not d:
            continue
        if exclude_date and d == exclude_date:
            continue
        sel = (row.get("selections") or {}).get(family) or {}
        pre = sel.get("pre_override_selected")
        if pre:
            by_date[d] = str(pre)
    if not by_date:
        return []
    ordered = sorted(by_date.items(), key=lambda kv: kv[0])
    tail = ordered[-window:]
    return [v for _, v in tail]


def _modal_selection(history_picks: Sequence[str]) -> Optional[str]:
    """Most-frequent selection. Returns None on tie or empty input.

    On tie, we deliberately return None so the gate falls back to today's
    pick rather than locking in an arbitrary tie-breaker.
    """
    if not history_picks:
        return None
    counts: Dict[str, int] = {}
    for v in history_picks:
        counts[v] = counts.get(v, 0) + 1
    sorted_counts = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    top_name, top_count = sorted_counts[0]
    if len(sorted_counts) > 1 and sorted_counts[1][1] == top_count:
        return None
    return top_name


def _apply_stability_gate(
    pre_override_selected: str,
    history_rows: List[Dict[str, Any]],
    family: str,
    *,
    window: int = DEFAULT_STABILITY_WINDOW,
    min_history: int = DEFAULT_STABILITY_MIN_HISTORY,
    exclude_date: Optional[str] = None,
) -> Tuple[str, Dict[str, Any]]:
    """Override today's calibration method if it differs from the trailing
    modal selection. Returns (final_method, audit_dict).

    The gate only fires when:
      - We have at least `min_history` distinct prior dates for this family.
      - The modal of the last `window` dates is unambiguous (no tie).
      - Today's pre-override selection differs from the modal.

    Otherwise today's pre-override pick passes through unchanged. This
    keeps validation-noise-induced flip-flops from churning the runtime
    calibrator while still allowing genuine drift through after several
    consistent days.
    """
    audit: Dict[str, Any] = {
        "stability_gate_enabled": True,
        "stability_window": window,
        "stability_min_history": min_history,
        "stability_history_count": 0,
        "stability_history": [],
        "stability_modal": None,
        "stability_gate_applied": False,
    }
    family_history = _trailing_family_history(
        history_rows, family, window=window, exclude_date=exclude_date
    )
    audit["stability_history_count"] = len(family_history)
    audit["stability_history"] = list(family_history)
    if len(family_history) < min_history:
        return pre_override_selected, audit
    modal = _modal_selection(family_history)
    audit["stability_modal"] = modal
    if modal is None or modal == pre_override_selected:
        return pre_override_selected, audit
    audit["stability_gate_applied"] = True
    audit["stability_override_from"] = pre_override_selected
    return modal, audit


def _load_input_drift_status(
    path: Path,
    *,
    min_features: int = INPUT_DRIFT_TRIGGER_MIN_MAJOR_FEATURES,
    psi_threshold: float = INPUT_DRIFT_TRIGGER_PSI_THRESHOLD,
) -> Dict[str, Any]:
    """Read the concept-drift report and decide whether the input
    distribution has drifted enough that today's calibration choice
    should be flagged. Returns a dict suitable for merging into
    `selection_audit`. Always returns a well-formed result -- missing
    or unreadable report falls back to `triggered=false` with a
    diagnostic `input_drift_status`.

    The trigger looks at CONTINUOUS features only (metric=='PSI');
    categorical TVD doesn't carry the same calibration-relevance
    signal.
    """
    base: Dict[str, Any] = {
        "input_drift_triggered": False,
        "input_drift_status": "ok",
        "input_drift_major_features": [],
        "input_drift_threshold": psi_threshold,
        "input_drift_min_features_to_trigger": min_features,
        "input_drift_report_generated_at_utc": None,
    }
    if not path.exists():
        base["input_drift_status"] = "report_missing"
        return base
    try:
        with open(path, "r", encoding="utf-8") as f:
            report = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        base["input_drift_status"] = "report_unreadable"
        base["input_drift_error"] = f"{exc!r}"
        return base
    base["input_drift_report_generated_at_utc"] = report.get("generated_at_utc")
    features = report.get("features") or {}
    major: List[Dict[str, Any]] = []
    for fname, info in features.items():
        # The report writes metric in lowercase ("psi"/"tvd") but tests
        # and docs often use uppercase -- normalise.
        metric = str(info.get("metric") or "").upper()
        if metric != "PSI":
            continue
        if str(info.get("verdict") or "") != "major":
            continue
        value = info.get("value")
        try:
            psi = float(value)
        except (TypeError, ValueError):
            continue
        major.append({"feature": str(fname), "psi": round(psi, 4)})
    major.sort(key=lambda r: r["psi"], reverse=True)
    base["input_drift_major_features"] = major
    if len(major) >= min_features:
        base["input_drift_triggered"] = True
    return base


def _write_selection_history_row(
    path: Path,
    *,
    selections: Dict[str, Dict[str, Any]],
    data_max_date: Optional[str],
    generated_at_utc: str,
) -> None:
    """Append one row to selection_history.jsonl. Creates the directory
    + file on first use. Atomic-append best-effort: if the write fails we
    log a warning but don't fail the calibration run."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        row = {
            "generated_at_utc": generated_at_utc,
            "data_max_date": data_max_date,
            "selections": selections,
        }
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(row) + "\n")
    except OSError as exc:
        LOGGER.warning(
            "Failed to append selection-history row to %s: %s. "
            "Calibration succeeded but the stability gate has nothing to "
            "read on the next refresh.",
            path, exc,
        )


def _fit_calibration_bundle(
    samples: List[Sample],
    *,
    input_path: Path,
    mode: str,
    min_date: str,
    max_date: str,
    val_frac: float,
    test_frac: float,
    input_kind: str,
    family_mode: str,
    model_family: Optional[str],
    strict: bool,
    skipped_reasons: Dict[str, int],
    probability_source_counts: Dict[str, int],
    artifact_purpose: str = "evaluation",
    identity_rejection_train_ece_delta: float = DEFAULT_IDENTITY_REJECTION_TRAIN_ECE_DELTA,
    stability_history: Optional[List[Dict[str, Any]]] = None,
    stability_window: int = DEFAULT_STABILITY_WINDOW,
    stability_min_history: int = DEFAULT_STABILITY_MIN_HISTORY,
    stability_gate_enabled: bool = True,
    input_drift_status: Optional[Dict[str, Any]] = None,
    side: str = "over",
) -> Tuple[Dict[str, Any], Dict[str, Any], List[Dict[str, Any]]]:
    split_dates = _split_dates([s.session_date for s in samples], val_frac, test_frac)
    split_map = {d: "train" for d in split_dates["train"]}
    split_map.update({d: "validation" for d in split_dates["validation"]})
    split_map.update({d: "test" for d in split_dates["test"]})
    for s in samples:
        s.split = split_map.get(s.session_date, "train")

    train = [s for s in samples if s.split == "train"]
    val = [s for s in samples if s.split == "validation"]
    test = [s for s in samples if s.split == "test"]

    if strict and len(train) < 20:
        family_msg = f" for {model_family}" if model_family else ""
        raise SystemExit(f"Strict mode failed: not enough train rows{family_msg} ({len(train)}).")
    if strict and len(set(s.label for s in train)) < 2:
        family_msg = f" for {model_family}" if model_family else ""
        raise SystemExit(f"Strict mode failed: train rows{family_msg} contain only one class.")

    train_probs = [s.raw_prob for s in train]
    train_labels = [s.label for s in train]
    val_probs = [s.raw_prob for s in val]
    val_labels = [s.label for s in val]
    test_probs = [s.raw_prob for s in test]
    test_labels = [s.label for s in test]

    platt_params = _fit_platt(train_probs, train_labels) if train else {"a": 1.0, "b": 0.0}
    isotonic_params = _fit_isotonic(train_probs, train_labels) if train else {"x": [0.0, 1.0], "y": [0.0, 1.0]}
    runtime_fit_scope = "train_split"
    export_platt_params = platt_params
    export_isotonic_params = isotonic_params
    if artifact_purpose == "runtime-refit":
        all_probs = [s.raw_prob for s in samples]
        all_labels = [s.label for s in samples]
        export_platt_params = _fit_platt(all_probs, all_labels) if samples else {"a": 1.0, "b": 0.0}
        export_isotonic_params = _fit_isotonic(all_probs, all_labels) if samples else {"x": [0.0, 1.0], "y": [0.0, 1.0]}
        runtime_fit_scope = "all_eligible_labeled_rows_after_method_selection"

    method_predictions: Dict[str, Dict[str, List[float]]] = {
        "raw": {
            "train": list(train_probs),
            "validation": list(val_probs),
            "test": list(test_probs),
        },
        "platt": {
            "train": _predict_platt(train_probs, platt_params),
            "validation": _predict_platt(val_probs, platt_params),
            "test": _predict_platt(test_probs, platt_params),
        },
        "isotonic": {
            "train": _predict_isotonic(train_probs, isotonic_params),
            "validation": _predict_isotonic(val_probs, isotonic_params),
            "test": _predict_isotonic(test_probs, isotonic_params),
        },
    }
    export_method_predictions: Dict[str, Dict[str, List[float]]] = method_predictions
    if artifact_purpose == "runtime-refit":
        export_method_predictions = {
            "raw": method_predictions["raw"],
            "platt": {
                "train": _predict_platt(train_probs, export_platt_params),
                "validation": _predict_platt(val_probs, export_platt_params),
                "test": _predict_platt(test_probs, export_platt_params),
            },
            "isotonic": {
                "train": _predict_isotonic(train_probs, export_isotonic_params),
                "validation": _predict_isotonic(val_probs, export_isotonic_params),
                "test": _predict_isotonic(test_probs, export_isotonic_params),
            },
        }

    method_eval: Dict[str, Dict[str, Any]] = {}
    for name in ["raw", "platt", "isotonic"]:
        method_eval[name] = {
            "train": _metrics_bundle(train_labels, train_probs, method_predictions[name]["train"]),
            "validation": _metrics_bundle(val_labels, val_probs, method_predictions[name]["validation"]),
            "test": _metrics_bundle(test_labels, test_probs, method_predictions[name]["test"]),
        }

    selected_method, selection_audit = _select_best_method(
        method_eval,
        identity_rejection_train_ece_delta=identity_rejection_train_ece_delta,
    )
    if selection_audit.get("identity_rejection_applied"):
        family_msg = f" for {model_family}" if model_family else ""
        LOGGER.warning(
            "Identity-rejection guard fired%s: validation picked 'raw' (logloss=%.4f) "
            "but train ECE shows '%s' is %.4f better calibrated (raw=%.4f vs "
            "challenger=%.4f, threshold=%.2f). Selected '%s'.",
            family_msg,
            float(selection_audit.get("primary_validation_logloss") or 0.0),
            selection_audit.get("challenger"),
            float(selection_audit.get("identity_rejection_train_ece_gap") or 0.0),
            float(selection_audit.get("raw_train_ece") or 0.0),
            float(selection_audit.get("challenger_train_ece") or 0.0),
            float(selection_audit.get("identity_rejection_threshold") or 0.0),
            selected_method,
        )

    # Stability gate: prevent platt<->isotonic flip-flop on noisy small
    # validation sets by overriding to the trailing modal selection.
    pre_override_method = selected_method
    selection_audit["pre_override_selected"] = pre_override_method
    if stability_gate_enabled and model_family and stability_history is not None:
        exclude_date = (max_date or "") and str(max_date)[:10]
        final_method, stability_audit = _apply_stability_gate(
            pre_override_method,
            stability_history,
            model_family,
            window=stability_window,
            min_history=stability_min_history,
            exclude_date=exclude_date or None,
        )
        selection_audit.update(stability_audit)
        if stability_audit.get("stability_gate_applied"):
            family_msg = f" for {model_family}" if model_family else ""
            LOGGER.warning(
                "Calibration stability gate fired%s: today's pick was '%s' but "
                "trailing %d-day modal is '%s' (history=%s). Overriding to '%s' "
                "to avoid daily flip-flop on small validation samples.",
                family_msg, pre_override_method,
                stability_audit.get("stability_window"),
                stability_audit.get("stability_modal"),
                stability_audit.get("stability_history"),
                final_method,
            )
        selected_method = final_method
    else:
        selection_audit["stability_gate_enabled"] = False

    # Input-drift audit: flag (without changing the selected method) when
    # the concept-drift report shows >= 2 continuous features at major
    # PSI. Tells operators today's calibration choice was made on inputs
    # whose distribution has shifted -- the stability gate they trust is
    # standing on shifting ground.
    if input_drift_status is not None:
        selection_audit.update(input_drift_status)
        if input_drift_status.get("input_drift_triggered"):
            family_msg = f" for {model_family}" if model_family else ""
            top = input_drift_status.get("input_drift_major_features") or []
            top_summary = ", ".join(
                f"{r['feature']} PSI {r['psi']:.2f}" for r in top[:5]
            )
            LOGGER.warning(
                "Input-drift flag set%s: %d continuous features at PSI >= %.2f "
                "(threshold trigger %d). Top: %s. Today's calibration method "
                "(%s) was selected on materially-shifted inputs.",
                family_msg,
                len(top),
                INPUT_DRIFT_TRIGGER_PSI_THRESHOLD,
                INPUT_DRIFT_TRIGGER_MIN_MAJOR_FEATURES,
                top_summary,
                selected_method,
            )

    family_counts: Dict[str, int] = {}
    for sample in samples:
        family_counts[sample.model_family] = family_counts.get(sample.model_family, 0) + 1

    calibration_payload = {
        "schema_version": 1,
        "generated_at_utc": _now_iso(),
        "selected_method": selected_method,
        "selection_metric": "validation_logloss",
        "selection_audit": selection_audit,
        "methods": {
            "raw": {
                "params": {},
                "metrics": method_eval["raw"],
            },
            "platt": {
                "params": export_platt_params,
                "evaluation_params": platt_params,
                "metrics": method_eval["platt"],
            },
            "isotonic": {
                "params": export_isotonic_params,
                "evaluation_params": isotonic_params,
                "metrics": method_eval["isotonic"],
            },
        },
        "side": side,
        "data": {
            "input_path": str(input_path),
            "input_kind": input_kind,
            "mode": mode,
            "side": side,
            "family_mode": family_mode,
            "model_family": model_family,
            "min_date": min_date or None,
            "max_date": max_date or None,
            "rows_total": len(samples),
            "rows_train": len(train),
            "rows_validation": len(val),
            "rows_test": len(test),
            "dates_by_split": split_dates,
            "skipped_reasons": skipped_reasons,
            "probability_source_counts": probability_source_counts,
            "model_family_counts": family_counts,
        },
        "artifact_purpose": artifact_purpose,
        "fit_scope": runtime_fit_scope,
        "selection_source": "train_validation_test_evaluation_split",
        "notes": {
            "label": (
                ("under_win = 1 - over_win; flipped from " if side == "under" else "")
                + "won_counterfactual for signals_master; "
                "target_counterfactual_win for candidate_universe; "
                "target_over_win (gated on label_final_available) for "
                "calibration_opportunity_training"
            ),
            "probability_field": (
                "1 - fair_value_raw (fallback: 1 - fair_value)"
                if side == "under"
                else "fair_value_raw (fallback: fair_value)"
            ),
            "selection_policy": "Best validation logloss among raw/platt/isotonic.",
            "runtime_modes": (
                "Phase A2: UNDER calibration is offline / shadow only "
                "until Phase B/C wire it into the live engine."
                if side == "under"
                else "Use with --prob-calibration-mode shadow|enforce."
            ),
        },
    }

    report_payload = {
        "generated_at_utc": _now_iso(),
        "selected_method": selected_method,
        "method_eval": method_eval,
        "artifact_purpose": artifact_purpose,
        "fit_scope": runtime_fit_scope,
        "evaluation_platt_params": platt_params,
        "evaluation_isotonic_params": isotonic_params,
        "export_platt_params": export_platt_params,
        "export_isotonic_params": export_isotonic_params,
        "dataset": calibration_payload["data"],
    }

    pred_rows: List[Dict[str, Any]] = []
    split_buckets = {"train": train, "validation": val, "test": test}
    for split_name, bucket in split_buckets.items():
        raw_ps = method_predictions["raw"][split_name]
        platt_ps = method_predictions["platt"][split_name]
        iso_ps = method_predictions["isotonic"][split_name]
        export_raw_ps = export_method_predictions["raw"][split_name]
        export_platt_ps = export_method_predictions["platt"][split_name]
        export_iso_ps = export_method_predictions["isotonic"][split_name]
        for i, s in enumerate(bucket):
            selected_prob_evaluation = (
                raw_ps[i]
                if selected_method == "identity"
                else platt_ps[i]
                if selected_method == "platt"
                else iso_ps[i]
            )
            selected_prob_runtime_refit = (
                export_raw_ps[i]
                if selected_method == "identity"
                else export_platt_ps[i]
                if selected_method == "platt"
                else export_iso_ps[i]
            )
            pred_rows.append(
                {
                    "bet_id": s.bet_id,
                    "session_date": s.session_date,
                    "mode": s.mode,
                    "model_family": s.model_family,
                    "split": split_name,
                    "label": s.label,
                    "raw_prob": raw_ps[i],
                    "raw_prob_source": s.raw_prob_source,
                    "platt_prob": platt_ps[i],
                    "isotonic_prob": iso_ps[i],
                    "selected_prob": selected_prob_evaluation,
                    "selected_prob_evaluation": selected_prob_evaluation,
                    "selected_prob_runtime_refit": selected_prob_runtime_refit,
                    "artifact_purpose": artifact_purpose,
                    "decision_ask": s.decision_ask,
                    "line": s.line,
                    "inning": s.inning,
                }
            )

    return calibration_payload, report_payload, pred_rows


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
    if args.val_frac < 0 or args.test_frac < 0:
        raise SystemExit("--val-frac and --test-frac must be >= 0")
    if args.val_frac + args.test_frac >= 1.0:
        raise SystemExit("--val-frac + --test-frac must be < 1.0")
    if args.model_family not in {"all", *KNOWN_MODEL_FAMILIES}:
        raise SystemExit(
            "--model-family must be all, score_event_transition, or no_score_drift"
        )
    if not args.input_path.exists():
        raise SystemExit(f"Input file not found: {args.input_path}")
    args.output_root.mkdir(parents=True, exist_ok=True)

    # Side-aware default resolution (Phase A2). When --side under and
    # the user did not pass --output-stem / --selection-history-path,
    # auto-route to the UNDER-specific defaults so OVER and UNDER
    # artifacts + stability-gate histories live in disjoint files.
    # Explicit user-passed values always win.
    if not args.output_stem:
        args.output_stem = (
            DEFAULT_OUTPUT_STEM_UNDER if args.side == "under"
            else DEFAULT_OUTPUT_STEM
        )
    if args.selection_history_path is None:
        args.selection_history_path = (
            DEFAULT_SELECTION_HISTORY_PATH_UNDER if args.side == "under"
            else DEFAULT_SELECTION_HISTORY_PATH
        )

    rows = _read_jsonl(args.input_path)
    samples: List[Sample] = []
    skipped_reasons: Dict[str, int] = {}
    probability_source_counts: Dict[str, int] = {}
    for row in rows:
        mode = str(row.get("mode") or "")
        if args.mode != "both" and mode != args.mode:
            skipped_reasons["mode_filter"] = skipped_reasons.get("mode_filter", 0) + 1
            continue
        session_date = _session_date(row)
        if not _date_in_range(session_date, args.min_date or None, args.max_date or None):
            skipped_reasons["date_filter"] = skipped_reasons.get("date_filter", 0) + 1
            continue

        model_family = infer_signal_model_family(row)
        if not _family_filter_passes(model_family, args.model_family):
            skipped_reasons["model_family_filter"] = skipped_reasons.get("model_family_filter", 0) + 1
            continue

        label, label_status = _label_for_row(row, args.input_kind)
        if label_status != "ok" or label is None:
            skipped_reasons[label_status] = skipped_reasons.get(label_status, 0) + 1
            continue

        raw_prob_source = "fair_value_raw"
        raw_prob = _safe_float(row.get("fair_value_raw"))
        if raw_prob is None:
            raw_prob_source = "fair_value"
            raw_prob = _safe_float(row.get("fair_value"))
        if raw_prob is None:
            skipped_reasons["missing_fair_value"] = skipped_reasons.get("missing_fair_value", 0) + 1
            continue
        if not (0.0 < raw_prob < 1.0):
            skipped_reasons["fair_value_out_of_bounds"] = skipped_reasons.get("fair_value_out_of_bounds", 0) + 1
            continue
        probability_source_counts[raw_prob_source] = probability_source_counts.get(raw_prob_source, 0) + 1
        # Phase A2: --side under flips the binary label (over_win=1 ->
        # under_win=0) and the OVER raw probability (p_over -> p_under
        # = 1 - p_over). Bounds-check + family-filter + label-status
        # gating all run against the Over row's metadata; only the
        # arithmetic flips. Decision_ask stays the Over ask: the
        # Under ask is not currently in the schema (Phase B will add
        # it as part of side-aware audit), but for calibration math
        # only the label + prob matter -- decision_ask is carried
        # forward only as diagnostic.
        if args.side == "under":
            label = 1 - label
            raw_prob = 1.0 - raw_prob
        samples.append(
            Sample(
                bet_id=str(row.get("bet_id") or ""),
                session_date=session_date,
                mode=mode,
                raw_prob=_clip_prob(raw_prob),
                raw_prob_source=raw_prob_source,
                label=label,
                model_family=model_family,
                decision_ask=_safe_float(row.get("decision_ask")),
                line=str(row.get("line")) if row.get("line") is not None else None,
                inning=_safe_int(row.get("inning")),
            )
        )

    if not samples:
        if args.strict:
            raise SystemExit("Strict mode failed: no eligible settled rows for calibration.")
        # Soft no-op: early in the season the candidate-universe / training
        # table can be empty. Surface a warning, keep any existing artifact in
        # place, and exit 0 so the daily refresh stays green.
        LOGGER.warning(
            "No eligible settled rows for calibration at %s; leaving any "
            "existing artifact under %s untouched. Skipped reasons: %s",
            args.input_path,
            args.output_root,
            skipped_reasons,
        )
        return

    # Load prior calibration-method selections once; the stability gate
    # reads it per family. Empty / missing file is a normal first run.
    stability_history = (
        _load_selection_history(Path(args.selection_history_path))
        if args.stability_gate_enabled else []
    )

    # Load concept-drift status once; the same audit is attached to every
    # family's selection_audit so a per-family inspection shows whether
    # today's selection was made on drifted inputs.
    input_drift_status = _load_input_drift_status(args.concept_drift_report_path)

    if args.family_mode == "pooled":
        calibration_payload, report_payload, pred_rows = _fit_calibration_bundle(
            samples,
            input_path=args.input_path,
            mode=args.mode,
            min_date=args.min_date,
            max_date=args.max_date,
            val_frac=args.val_frac,
            test_frac=args.test_frac,
            input_kind=args.input_kind,
            family_mode=args.family_mode,
            model_family=None if args.model_family == "all" else args.model_family,
            strict=args.strict,
            skipped_reasons=skipped_reasons,
            probability_source_counts=probability_source_counts,
            artifact_purpose=args.artifact_purpose,
            identity_rejection_train_ece_delta=args.identity_rejection_train_ece_delta,
            stability_history=stability_history,
            stability_window=args.stability_window,
            stability_min_history=args.stability_min_history,
            stability_gate_enabled=args.stability_gate_enabled,
            input_drift_status=input_drift_status,
            side=args.side,
        )
    else:
        by_family: Dict[str, List[Sample]] = {}
        for sample in samples:
            by_family.setdefault(sample.model_family, []).append(sample)

        family_payloads: Dict[str, Dict[str, Any]] = {}
        family_reports: Dict[str, Dict[str, Any]] = {}
        pred_rows = []
        for family in sorted(by_family.keys()):
            family_payload, family_report, family_preds = _fit_calibration_bundle(
                list(by_family[family]),
                input_path=args.input_path,
                mode=args.mode,
                min_date=args.min_date,
                max_date=args.max_date,
                val_frac=args.val_frac,
                test_frac=args.test_frac,
                input_kind=args.input_kind,
                family_mode=args.family_mode,
                model_family=family,
                strict=args.strict,
                skipped_reasons={},
                probability_source_counts=probability_source_counts,
                artifact_purpose=args.artifact_purpose,
                identity_rejection_train_ece_delta=args.identity_rejection_train_ece_delta,
                stability_history=stability_history,
                stability_window=args.stability_window,
                stability_min_history=args.stability_min_history,
                stability_gate_enabled=args.stability_gate_enabled,
                input_drift_status=input_drift_status,
                side=args.side,
            )
            family_payloads[family] = family_payload
            family_reports[family] = family_report
            pred_rows.extend(family_preds)

        default_family = str(args.default_family or SCORE_EVENT_TRANSITION)
        if default_family not in family_payloads:
            default_family = SCORE_EVENT_TRANSITION if SCORE_EVENT_TRANSITION in family_payloads else sorted(family_payloads)[0]
        default_payload = family_payloads[default_family]
        family_counts = {family: len(rows_for_family) for family, rows_for_family in sorted(by_family.items())}
        calibration_payload = {
            "schema_version": 2,
            "generated_at_utc": _now_iso(),
            "side": args.side,
            "family_mode": "separate",
            "default_family": default_family,
            "selected_method": default_payload["selected_method"],
            "selection_metric": "validation_logloss",
            "methods": default_payload["methods"],
            "families": family_payloads,
            "artifact_purpose": args.artifact_purpose,
            "fit_scope": (
                "all_eligible_labeled_rows_after_method_selection"
                if args.artifact_purpose == "runtime-refit"
                else "train_split"
            ),
            "selection_source": "train_validation_test_evaluation_split",
            "data": {
                "input_path": str(args.input_path),
                "input_kind": args.input_kind,
                "mode": args.mode,
                "side": args.side,
                "model_family": None if args.model_family == "all" else args.model_family,
                "known_model_families": list(KNOWN_MODEL_FAMILIES),
                "min_date": args.min_date or None,
                "max_date": args.max_date or None,
                "artifact_purpose": args.artifact_purpose,
                "rows_total": len(samples),
                "skipped_reasons": skipped_reasons,
                "probability_source_counts": probability_source_counts,
                "model_family_counts": family_counts,
            },
            "notes": {
                "label": (
                    ("under_win = 1 - over_win; flipped from " if args.side == "under" else "")
                    + "won_counterfactual for signals_master; "
                    "target_counterfactual_win for candidate_universe; "
                    "target_over_win (gated on label_final_available) for "
                    "calibration_opportunity_training"
                ),
                "probability_field": (
                    "1 - fair_value_raw (fallback: 1 - fair_value)"
                    if args.side == "under"
                    else "fair_value_raw (fallback: fair_value)"
                ),
                "selection_policy": "Each family selects best validation logloss among raw/platt/isotonic independently.",
                "runtime_modes": (
                    "Phase A2: UNDER calibration is offline / shadow only "
                    "until Phase B/C wire it into the live engine."
                    if args.side == "under"
                    else "Runtime routes by signal_model_family; callers without a family use default_family."
                ),
            },
        }
        report_payload = {
            "generated_at_utc": _now_iso(),
            "side": args.side,
            "family_mode": "separate",
            "default_family": default_family,
            "artifact_purpose": args.artifact_purpose,
            "fit_scope": calibration_payload["fit_scope"],
            "family_reports": family_reports,
            "dataset": calibration_payload["data"],
        }

    # Active #16 (2026-05-17): stamp build-time lineage on both the
    # calibration artifact and its report. The fast Wilson-UB demote
    # check (#13) flags a failing post-promotion ROI within 5-6 days;
    # when that fires, the operator's first question is "which
    # calibrator was in production?" Lineage answers that without
    # git-log archaeology.
    try:
        from scripts.analysis.artifact_lineage import compute_lineage as _compute_lineage
    except ImportError:
        try:
            from artifact_lineage import compute_lineage as _compute_lineage  # type: ignore[no-redef]
        except ImportError:
            _compute_lineage = None  # type: ignore[assignment]
    if _compute_lineage is not None:
        artifact_lineage = _compute_lineage(
            builder_path=__file__,
            input_paths=[args.input_path, args.concept_drift_report_path],
            project_root=PROJECT_DIR,
            extra={
                "cli_args_summary": {
                    "side": getattr(args, "side", "over"),
                    "family_mode": args.family_mode,
                    "model_family": args.model_family,
                    "mode": args.mode,
                    "artifact_purpose": args.artifact_purpose,
                    "stability_gate_enabled": args.stability_gate_enabled,
                },
            },
        )
        calibration_payload["lineage"] = artifact_lineage
        report_payload["lineage"] = artifact_lineage

    calibration_path = args.output_root / f"{args.output_stem}.json"
    report_path = args.output_root / f"{args.output_stem}_report.json"
    predictions_path = args.output_root / f"{args.output_stem}_predictions.jsonl"
    _write_json(calibration_path, calibration_payload)
    _write_json(report_path, report_payload)
    _write_jsonl(predictions_path, pred_rows)

    LOGGER.info("Wrote %s", calibration_path)
    LOGGER.info("Wrote %s", report_path)
    LOGGER.info("Wrote %s", predictions_path)
    LOGGER.info(
        "Calibration rows: total=%d family_mode=%s families=%s selected=%s",
        len(samples),
        args.family_mode,
        calibration_payload.get("data", {}).get("model_family_counts"),
        calibration_payload.get("selected_method"),
    )

    # Append today's per-family selections to the stability-gate history.
    # We record BOTH the pre-override pick (what today's data wanted) and
    # the final pick (what we shipped). The gate reads pre-override to
    # compute the modal, so override-locked days don't self-reinforce.
    if args.stability_gate_enabled:
        if args.family_mode == "pooled":
            audit = (calibration_payload.get("selection_audit") or {})
            selections_for_history: Dict[str, Dict[str, Any]] = {
                "_pooled_": {
                    "pre_override_selected": audit.get(
                        "pre_override_selected",
                        calibration_payload.get("selected_method"),
                    ),
                    "final_selected": calibration_payload.get("selected_method"),
                    "stability_gate_applied": bool(audit.get("stability_gate_applied", False)),
                }
            }
        else:
            selections_for_history = {}
            for family, family_payload in (calibration_payload.get("families") or {}).items():
                audit = (family_payload.get("selection_audit") or {})
                selections_for_history[family] = {
                    "pre_override_selected": audit.get(
                        "pre_override_selected",
                        family_payload.get("selected_method"),
                    ),
                    "final_selected": family_payload.get("selected_method"),
                    "stability_gate_applied": bool(audit.get("stability_gate_applied", False)),
                }
        _write_selection_history_row(
            Path(args.selection_history_path),
            selections=selections_for_history,
            data_max_date=(args.max_date or None),
            generated_at_utc=_now_iso(),
        )


if __name__ == "__main__":
    main()
