"""Calibration fitting + scoring methods (Platt, isotonic) plus the
metrics bundle and best-method selector. Extracted from
calibrate_signal_probabilities.py on 2026-05-25 as part of Tier 2.

Public surface (also re-exported by calibrate_signal_probabilities for
back-compat): _fit_platt, _predict_platt, _fit_isotonic,
_predict_isotonic, _metrics_bundle, _select_best_method.
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .scoring import (
    _logit, _clip_prob, _stable_sigmoid,
    _logloss, _brier, _ece, _reliability_bins, _slice_overconfidence,
)


DEFAULT_IDENTITY_REJECTION_TRAIN_ECE_DELTA = 0.05

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

