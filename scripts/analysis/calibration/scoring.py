"""Pure scoring/calibration math helpers used by methods.py and the
calibration bundle in calibrate_signal_probabilities.py.

Extracted 2026-05-25.
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Sequence


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


