"""
ev_policy.py -- EV policy model runtime scoring.

Loads trained model artifacts produced by scripts/analysis/backtest_ev_policy.py
and scores signal feature vectors to estimate P(win|filled) and P(fill).

This module is a pure ML inference component: it has no trading logic, no
CLOB knowledge, and no dependency on the signal engine or bet models.
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _safe_float(v: Any) -> Optional[float]:
    try:
        if v is None or v == "":
            return None
        return float(v)
    except Exception:
        return None


def _is_missing(v: Any) -> bool:
    return v is None or v == ""


def _stable_sigmoid(x: float) -> float:
    if x >= 0:
        z = math.exp(-x)
        return 1.0 / (1.0 + z)
    z = math.exp(x)
    return z / (1.0 + z)


# ---------------------------------------------------------------------------
# Scorer
# ---------------------------------------------------------------------------

class LogisticJsonScorer:
    """
    Runtime scorer for model artifacts emitted by backtest_ev_policy.py.

    The artifact JSON contains a preprocessor spec (column lists, medians,
    means, stds, one-hot category lists) and a logistic model (bias + weights).
    Calling score() on a raw feature row returns a calibrated probability.
    """

    def __init__(self, payload: Dict[str, Any]) -> None:
        pp = payload.get("preprocessor", {})
        mdl = payload.get("model", {})

        self.numeric_cols: List[str] = list(pp.get("numeric_cols", []))
        self.categorical_cols: List[str] = list(pp.get("categorical_cols", []))
        self.medians: Dict[str, float] = {
            str(k): float(v) for k, v in dict(pp.get("medians", {})).items()
        }
        self.means: Dict[str, float] = {
            str(k): float(v) for k, v in dict(pp.get("means", {})).items()
        }
        self.stds: Dict[str, float] = {
            str(k): float(v) for k, v in dict(pp.get("stds", {})).items()
        }
        self.categories: Dict[str, List[str]] = {
            str(k): [str(x) for x in vals]
            for k, vals in dict(pp.get("categories", {})).items()
        }
        self.feature_names: List[str] = list(pp.get("feature_names", []))

        self.is_constant = bool(mdl.get("is_constant"))
        self.bias = float(mdl.get("bias", 0.0) or 0.0)
        self.constant_prob = float(mdl.get("constant_prob", 0.5) or 0.5)

        weight_rows = payload.get("weights", [])
        weight_map: Dict[str, float] = {}
        self.weight_feature_names: List[str] = []
        for row in weight_rows:
            if not isinstance(row, dict):
                continue
            name = str(row.get("feature", ""))
            if not name:
                continue
            self.weight_feature_names.append(name)
            weight_map[name] = float(row.get("weight", 0.0) or 0.0)
        self.weights: List[float] = [weight_map.get(name, 0.0) for name in self.feature_names]

        self.feature_cols_used: List[str] = [str(x) for x in payload.get("feature_cols_used", [])]
        self._validate_schema()

    def _expected_feature_count(self) -> int:
        return len(self.numeric_cols) + sum(
            len(self.categories.get(col, [])) for col in self.categorical_cols
        )

    def _validate_schema(self) -> None:
        """Reject artifacts whose preprocessor and model vector schemas disagree."""
        if self.is_constant:
            return
        expected = self._expected_feature_count()
        if expected != len(self.feature_names):
            raise ValueError(
                "EV model schema mismatch: preprocessor expands to "
                f"{expected} features but artifact lists {len(self.feature_names)} feature_names"
            )
        if len(self.weights) != len(self.feature_names):
            raise ValueError(
                "EV model schema mismatch: "
                f"{len(self.weights)} weights for {len(self.feature_names)} feature_names"
            )
        if len(self.weight_feature_names) != len(set(self.weight_feature_names)):
            raise ValueError("EV model schema mismatch: duplicate feature weights in artifact")
        if set(self.weight_feature_names) != set(self.feature_names):
            missing = sorted(set(self.feature_names) - set(self.weight_feature_names))
            extra = sorted(set(self.weight_feature_names) - set(self.feature_names))
            raise ValueError(
                "EV model schema mismatch: weight features do not match feature_names "
                f"(missing={missing[:5]} extra={extra[:5]})"
            )

    def required_input_cols(self) -> List[str]:
        """Raw row columns required by this artifact's preprocessor."""
        return list(self.numeric_cols) + list(self.categorical_cols)

    def missing_input_cols(self, row: Dict[str, Any]) -> List[str]:
        """Return required raw columns that are STRUCTURALLY ABSENT from the row.

        Only flags columns whose key is missing from the row entirely. Columns
        that are present-but-null are deliberately allowed through: the scorer
        median-imputes them in ``_row_to_vector`` (numeric) or treats them as
        the empty category (categorical). The reason we separate these two
        conditions:

        - Structural absence means the trainer used a column the runtime never
          surfaces -- a real schema gap that should warn and fail closed under
          enforce mode.
        - Value-null means the runtime computed nothing for this tick (e.g.
          no_score_drift candidates legitimately have no empirical state-value
          lookup). The artifact was fit with those nulls present in training
          data, so median imputation is the contract; flagging them here would
          conflate "feature not implemented" with "feature not applicable to
          this signal".
        """
        missing: List[str] = []
        for col in self.numeric_cols:
            if col not in row:
                missing.append(col)
        for col in self.categorical_cols:
            if col not in row:
                missing.append(col)
        return missing

    def _row_to_vector(self, row: Dict[str, Any]) -> List[float]:
        vec: List[float] = []
        for col in self.numeric_cols:
            val = _safe_float(row.get(col))
            if val is None:
                val = self.medians.get(col, 0.0)
            mean = self.means.get(col, 0.0)
            std = self.stds.get(col, 1.0)
            if abs(std) < 1e-12:
                std = 1.0
            vec.append((val - mean) / std)
        for col in self.categorical_cols:
            raw = row.get(col)
            sval = "" if raw is None else str(raw).strip()
            for cat in self.categories.get(col, []):
                vec.append(1.0 if sval == cat else 0.0)
        return vec

    def score(self, row: Dict[str, Any]) -> float:
        if self.is_constant:
            return min(max(self.constant_prob, 1e-8), 1.0 - 1e-8)
        vec = self._row_to_vector(row)
        if len(vec) != len(self.weights):
            raise ValueError(
                "EV model runtime vector mismatch: "
                f"row encoded to {len(vec)} features but model has {len(self.weights)} weights"
            )
        z = self.bias + sum(v * w for v, w in zip(vec, self.weights))
        return min(max(_stable_sigmoid(z), 1e-8), 1.0 - 1e-8)
