#!/usr/bin/env python3
"""
calibrate_team_offense_v2.py -- Phase 3+4 of team-offense V2 calibration.

Walks five candidate models up the complexity ladder and scores each on a
time-respecting train/val/test split:

  Train      2021-2024
  Validation 2025
  Test       2026

Predict target: `over_hit` per (game, half-inning, line) row, with
`logit(base_fv_stage1_plus_stage2)` entered as an offset so we measure the
incremental contribution of Stage-3.

  Baseline A    no Stage-3 (offset only)
  Baseline B    current Stage-3 v1 (LOGIT_DELTA_PER_RUN=0.20, 50-game raw,
                hard clamps, linear inning weight)
  Model 1       same shape as Baseline B, refit constant beta
  Model 2       Model 1 with EB-shrunk rolling_50 (no hard clamp)
  Model 3       linear blend of prior-season / season-to-date / momentum_10,
                EB-shrunk per window, refit constant beta
  Model 4       Model 3 + per-inning multiplicative weights (non-parametric
                replacement for the linear (9-inn)/8 ramp)

Empirical-Bayes shrinkage uses sigma_within^2 estimated from team-game
residuals on training years only, and tau^2 estimated per window from the
across-team variance of leakage-free rolling estimates on training years.

Outputs:
  data/analysis_output/team_offense_calibration/phase4_models.json
  data/analysis_output/team_offense_calibration/phase4_predictions_test.jsonl
"""

from __future__ import annotations

import argparse
import json
import logging
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
from scipy.optimize import minimize

PROJECT_DIR = Path(__file__).resolve().parents[2]
DEFAULT_TABLE = PROJECT_DIR / "data" / "analysis_output" / "team_offense_calibration" / "training_table.jsonl"
DEFAULT_FEATURES = PROJECT_DIR / "data" / "analysis_output" / "team_offense_calibration" / "team_features.jsonl"
DEFAULT_OUTPUT_DIR = PROJECT_DIR / "data" / "analysis_output" / "team_offense_calibration"
DEFAULT_GAME_LOG = PROJECT_DIR / "cache" / "team_game_log.json"

LOGGER = logging.getLogger("calibrate_team_offense_v2")
EPS = 1e-6

# Production-v1 hyperparameters (for Baseline B reproduction).
V1_LOGIT_DELTA_PER_RUN = 0.20
V1_MAX_LOGIT_DELTA = 0.60
V1_RPG_CLAMP_LOW_RATIO = 0.55
V1_RPG_CLAMP_HIGH_RATIO = 1.55

TRAIN_SEASONS = ("2021", "2022", "2023", "2024")
VAL_SEASONS = ("2025",)
TEST_SEASONS = ("2026",)


# ---------------------------------------------------------------------------
# Loading + joining
# ---------------------------------------------------------------------------


def _logit_safe(p: np.ndarray) -> np.ndarray:
    p = np.clip(p, EPS, 1.0 - EPS)
    return np.log(p / (1.0 - p))


def _sigmoid(x: np.ndarray) -> np.ndarray:
    return 1.0 / (1.0 + np.exp(-x))


def load_features(path: Path) -> Dict[Tuple[str, str], Dict[str, Optional[float]]]:
    out: Dict[Tuple[str, str], Dict[str, Optional[float]]] = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            r = json.loads(line)
            team = r.pop("team")
            date = r.pop("date")
            out[(team, date)] = r
    return out


def load_mlb_avg(game_log_path: Path) -> float:
    with open(game_log_path, encoding="utf-8") as f:
        d = json.load(f)
    return float(d.get("mlb_avg_rpg", 4.45))


def load_table_join_features(
    table_path: Path,
    features: Dict[Tuple[str, str], Dict[str, Optional[float]]],
    required_keys: List[str],
) -> Dict[str, np.ndarray]:
    """
    Stream the calibration table and emit numpy arrays for the columns we need.
    Drop rows where any of the required (away+home) feature keys is missing.
    """
    cols: Dict[str, List[Any]] = {
        "season": [], "inning": [], "line": [], "y": [], "logit_p": [], "p": [],
    }
    for k in required_keys:
        cols[f"away_{k}"] = []
        cols[f"home_{k}"] = []

    n_total = 0
    n_kept = 0
    with open(table_path, encoding="utf-8") as f:
        for line in f:
            r = json.loads(line)
            n_total += 1
            p = r.get("base_fv_stage1_plus_stage2")
            if p is None:
                continue
            af = features.get((r["away"], r["date"]))
            hf = features.get((r["home"], r["date"]))
            if af is None or hf is None:
                continue
            keep = True
            vals_a, vals_h = {}, {}
            for k in required_keys:
                a = af.get(k)
                h = hf.get(k)
                if a is None or h is None:
                    keep = False
                    break
                vals_a[k] = float(a)
                vals_h[k] = float(h)
            if not keep:
                continue
            cols["season"].append(r["season"])
            cols["inning"].append(int(r["inning"]))
            cols["line"].append(float(r["line"]))
            cols["y"].append(int(r["over_hit"]))
            cols["p"].append(float(p))
            cols["logit_p"].append(math.log(max(EPS, min(1 - EPS, p)) / (1 - max(EPS, min(1 - EPS, p)))))
            for k in required_keys:
                cols[f"away_{k}"].append(vals_a[k])
                cols[f"home_{k}"].append(vals_h[k])
            n_kept += 1
            if n_total % 200000 == 0:
                LOGGER.info("  scan: %d rows seen, %d kept", n_total, n_kept)

    LOGGER.info("Joined: %d kept of %d total (%.1f%%)", n_kept, n_total, 100.0 * n_kept / max(1, n_total))

    arr: Dict[str, np.ndarray] = {}
    for k, v in cols.items():
        if k == "season":
            arr[k] = np.array(v)
        elif k in ("y", "inning"):
            arr[k] = np.array(v, dtype=np.int32)
        else:
            arr[k] = np.array(v, dtype=np.float64)
    return arr


# ---------------------------------------------------------------------------
# EB shrinkage
# ---------------------------------------------------------------------------


def estimate_within_variance(table_path: Path, features: Dict, mlb_avg: float, train_seasons: Tuple[str, ...]) -> float:
    """
    sigma_within^2 = expected variance of single-game runs around team's
    long-run RPG. Estimated as variance of (runs_scored - rolling_50) on
    training-season game-level data.
    """
    # Reuse table - take final scores per game; or use the game log. Simpler:
    # walk the calibration table and treat (team, date, away_score_at_end of
    # game) as the "runs scored" -- but that's tricky since we have per-half
    # rows. Use game_log directly.
    return _estimate_within_variance_from_log(features, mlb_avg, train_seasons)


def _estimate_within_variance_from_log(
    features: Dict[Tuple[str, str], Dict[str, Optional[float]]],
    mlb_avg: float,
    train_seasons: Tuple[str, ...],
) -> float:
    """sigma^2 = mean( (game_runs - rolling_50_on_that_date)^2 )."""
    log_path = PROJECT_DIR / "cache" / "team_game_log.json"
    with open(log_path, encoding="utf-8") as f:
        d = json.load(f)
    sq = []
    for g in d["games"]:
        if g["date"][:4] not in train_seasons:
            continue
        for team, runs in [(g["away"], int(g["away_runs"])), (g["home"], int(g["home_runs"]))]:
            f_row = features.get((team, g["date"]))
            if not f_row:
                continue
            r50 = f_row.get("rolling_rpg_50")
            if r50 is None:
                continue
            sq.append((runs - float(r50)) ** 2)
    if not sq:
        return 10.0  # safe default; matches Phase 2b baseline MSE
    return float(sum(sq) / len(sq))


def estimate_tau_sq(features: Dict, train_seasons: Tuple[str, ...], window_key: str, n_window: int, sigma_sq: float) -> float:
    """
    tau^2 = across-team variance of true team RPG.
    Method-of-moments: var(observed_window) ~= tau^2 + sigma^2/n_window.
    => tau^2 = max(0, var(observed) - sigma^2 / n_window).
    """
    vals = []
    for (_team, date), feat in features.items():
        if date[:4] not in train_seasons:
            continue
        v = feat.get(window_key)
        if v is None:
            continue
        vals.append(float(v))
    if len(vals) < 2:
        return 0.4  # fallback
    mu = sum(vals) / len(vals)
    var = sum((v - mu) ** 2 for v in vals) / (len(vals) - 1)
    return max(1e-3, var - sigma_sq / max(1, n_window))


def shrink(values: np.ndarray, n_window: int, mu_league: float, sigma_sq: float, tau_sq: float) -> np.ndarray:
    """rpg_shrunk = (n / (n+k)) * y + (k / (n+k)) * mu, with k = sigma^2 / tau^2."""
    k = sigma_sq / max(1e-6, tau_sq)
    w = n_window / (n_window + k)
    return w * values + (1 - w) * mu_league


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


def linear_inning_weight(inning: np.ndarray) -> np.ndarray:
    """v1 production: max(0, (9-inn)/8)."""
    return np.maximum(0.0, (9.0 - inning.astype(np.float64)) / 8.0)


def predict_baselineA(data: Dict[str, np.ndarray]) -> np.ndarray:
    return data["p"]


def predict_baselineB(data: Dict[str, np.ndarray], mlb_avg: float) -> np.ndarray:
    """Reproduce production v1 Stage-3 exactly."""
    away = np.clip(data["away_rolling_rpg_50"], V1_RPG_CLAMP_LOW_RATIO * mlb_avg, V1_RPG_CLAMP_HIGH_RATIO * mlb_avg)
    home = np.clip(data["home_rolling_rpg_50"], V1_RPG_CLAMP_LOW_RATIO * mlb_avg, V1_RPG_CLAMP_HIGH_RATIO * mlb_avg)
    diff = (away + home) - 2.0 * mlb_avg
    w = linear_inning_weight(data["inning"])
    delta = np.clip(V1_LOGIT_DELTA_PER_RUN * diff * w, -V1_MAX_LOGIT_DELTA, V1_MAX_LOGIT_DELTA)
    return _sigmoid(data["logit_p"] + delta)


def fit_model_1(train: Dict[str, np.ndarray], mlb_avg: float) -> Dict[str, float]:
    """delta = beta * diff_v1 * w_lin. Solve for beta by Brier minimization."""
    away = np.clip(train["away_rolling_rpg_50"], V1_RPG_CLAMP_LOW_RATIO * mlb_avg, V1_RPG_CLAMP_HIGH_RATIO * mlb_avg)
    home = np.clip(train["home_rolling_rpg_50"], V1_RPG_CLAMP_LOW_RATIO * mlb_avg, V1_RPG_CLAMP_HIGH_RATIO * mlb_avg)
    diff = (away + home) - 2.0 * mlb_avg
    w = linear_inning_weight(train["inning"])
    feat = diff * w
    beta = _fit_logistic_offset(feat[:, None], train["y"], train["logit_p"])
    return {"beta_diff": float(beta[0])}


def predict_model_1(data: Dict[str, np.ndarray], params: Dict[str, float], mlb_avg: float) -> np.ndarray:
    away = np.clip(data["away_rolling_rpg_50"], V1_RPG_CLAMP_LOW_RATIO * mlb_avg, V1_RPG_CLAMP_HIGH_RATIO * mlb_avg)
    home = np.clip(data["home_rolling_rpg_50"], V1_RPG_CLAMP_LOW_RATIO * mlb_avg, V1_RPG_CLAMP_HIGH_RATIO * mlb_avg)
    diff = (away + home) - 2.0 * mlb_avg
    w = linear_inning_weight(data["inning"])
    delta = params["beta_diff"] * diff * w
    return _sigmoid(data["logit_p"] + delta)


def fit_model_2(train: Dict[str, np.ndarray], mlb_avg: float, shrunk_keys: Dict[str, str]) -> Dict[str, float]:
    """delta = beta * diff_shrunk * w_lin (no clamp; EB shrinkage replaces it)."""
    a = train[shrunk_keys["away"]]
    h = train[shrunk_keys["home"]]
    diff = (a + h) - 2.0 * mlb_avg
    w = linear_inning_weight(train["inning"])
    feat = diff * w
    beta = _fit_logistic_offset(feat[:, None], train["y"], train["logit_p"])
    return {"beta_diff": float(beta[0])}


def predict_model_2(data: Dict[str, np.ndarray], params: Dict[str, float], mlb_avg: float, shrunk_keys: Dict[str, str]) -> np.ndarray:
    a = data[shrunk_keys["away"]]
    h = data[shrunk_keys["home"]]
    diff = (a + h) - 2.0 * mlb_avg
    w = linear_inning_weight(data["inning"])
    delta = params["beta_diff"] * diff * w
    return _sigmoid(data["logit_p"] + delta)


def fit_model_3(train: Dict[str, np.ndarray], mlb_avg: float, shrunk_keys: Dict[str, Dict[str, str]]) -> Dict[str, float]:
    """
    delta = (b_prior * d_prior + b_season * d_season + b_momentum * d_momentum) * w_lin
    where each d_X is the (away+home - 2*mu) shrunk diff.
    """
    feats = []
    for fname in ["prior", "season", "momentum"]:
        a = train[shrunk_keys[fname]["away"]]
        h = train[shrunk_keys[fname]["home"]]
        feats.append((a + h) - 2.0 * mlb_avg)
    w = linear_inning_weight(train["inning"])
    X = np.stack([f * w for f in feats], axis=1)
    betas = _fit_logistic_offset(X, train["y"], train["logit_p"])
    return {"beta_prior": float(betas[0]), "beta_season": float(betas[1]), "beta_momentum": float(betas[2])}


def predict_model_3(data: Dict[str, np.ndarray], params: Dict[str, float], mlb_avg: float, shrunk_keys: Dict[str, Dict[str, str]]) -> np.ndarray:
    feats = []
    for fname in ["prior", "season", "momentum"]:
        a = data[shrunk_keys[fname]["away"]]
        h = data[shrunk_keys[fname]["home"]]
        feats.append((a + h) - 2.0 * mlb_avg)
    w = linear_inning_weight(data["inning"])
    delta = (params["beta_prior"] * feats[0] + params["beta_season"] * feats[1] +
             params["beta_momentum"] * feats[2]) * w
    return _sigmoid(data["logit_p"] + delta)


def fit_model_4(train: Dict[str, np.ndarray], mlb_avg: float, shrunk_keys: Dict[str, Dict[str, str]]) -> Dict[str, Any]:
    """
    Model 3 with per-inning multiplicative scalar w[inn] replacing the linear
    ramp. Innings 1-9; extras shrink linearly toward 0. Each inning gets its
    own scalar (10 free parameters), but to reduce dimensionality we let
    innings 10+ share weight w[10].
    """
    # Build per-(row, inning_index) one-hot, then learn:
    #   logit(p) = offset + sum_inn 1{inn=i} * (b_prior * d_prior + b_season * d_season + b_momentum * d_momentum) * w_inn[i]
    # Reparameterize: define w_inn per inning [1..9, 10+] and require
    # b_prior, b_season, b_momentum global. Identifiability: w_1 fixed to 1.0.
    inn = train["inning"].copy()
    inn_clipped = np.minimum(inn, 10)  # 10 = "10+"
    n_innings = 10
    inn_idx = inn_clipped - 1  # 0..9

    feats = []
    for fname in ["prior", "season", "momentum"]:
        a = train[shrunk_keys[fname]["away"]]
        h = train[shrunk_keys[fname]["home"]]
        feats.append((a + h) - 2.0 * mlb_avg)
    F = np.stack(feats, axis=1)  # (N, 3)
    y = train["y"].astype(np.float64)
    offset = train["logit_p"]

    # Parameter vector: [b_prior, b_season, b_momentum, w_2, w_3, ..., w_10]  (w_1 = 1)
    # 3 + 9 = 12 params.
    def unpack(theta):
        b = theta[:3]
        w = np.concatenate([[1.0], theta[3:]])
        return b, w

    def neg_loglik(theta):
        b, w = unpack(theta)
        diff = F @ b  # (N,)
        delta = diff * w[inn_idx]
        z = offset + delta
        # log-loss
        log_p1 = np.where(z > 0, -np.log1p(np.exp(-z)), z - np.log1p(np.exp(z)))
        log_p0 = np.where(z > 0, -z - np.log1p(np.exp(-z)), -np.log1p(np.exp(z)))
        return -np.sum(y * log_p1 + (1 - y) * log_p0)

    theta0 = np.concatenate([[0.06, 0.06, 0.06], np.linspace(0.95, 0.0, 9)])
    res = minimize(neg_loglik, theta0, method="L-BFGS-B")
    b, w = unpack(res.x)
    return {
        "beta_prior": float(b[0]),
        "beta_season": float(b[1]),
        "beta_momentum": float(b[2]),
        "inning_weights": [1.0] + [float(x) for x in w[1:]],
        "n_innings_grouped": n_innings,
    }


def predict_model_4(data: Dict[str, np.ndarray], params: Dict[str, Any], mlb_avg: float, shrunk_keys: Dict[str, Dict[str, str]]) -> np.ndarray:
    inn = np.minimum(data["inning"], 10) - 1
    feats = []
    for fname in ["prior", "season", "momentum"]:
        a = data[shrunk_keys[fname]["away"]]
        h = data[shrunk_keys[fname]["home"]]
        feats.append((a + h) - 2.0 * mlb_avg)
    F = np.stack(feats, axis=1)
    b = np.array([params["beta_prior"], params["beta_season"], params["beta_momentum"]])
    w = np.array(params["inning_weights"])
    diff = F @ b
    delta = diff * w[inn]
    return _sigmoid(data["logit_p"] + delta)


# ---------------------------------------------------------------------------
# Logistic regression with offset
# ---------------------------------------------------------------------------


def _fit_logistic_offset(X: np.ndarray, y: np.ndarray, offset: np.ndarray) -> np.ndarray:
    """Solve coefficients that minimize log-loss with logit offset.
       logit(p) = offset + X @ beta.   X is (N, k), y is (N,) in {0,1}.
    """
    N, k = X.shape
    y = y.astype(np.float64)

    def neg_loglik(beta):
        z = offset + X @ beta
        log_p1 = np.where(z > 0, -np.log1p(np.exp(-z)), z - np.log1p(np.exp(z)))
        log_p0 = np.where(z > 0, -z - np.log1p(np.exp(-z)), -np.log1p(np.exp(z)))
        return -np.sum(y * log_p1 + (1 - y) * log_p0)

    def grad(beta):
        z = offset + X @ beta
        p = 1.0 / (1.0 + np.exp(-z))
        return X.T @ (p - y)

    res = minimize(neg_loglik, np.zeros(k), jac=grad, method="L-BFGS-B")
    return res.x


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


def brier(y: np.ndarray, p: np.ndarray) -> float:
    return float(np.mean((p - y) ** 2))


def log_loss(y: np.ndarray, p: np.ndarray) -> float:
    p = np.clip(p, EPS, 1 - EPS)
    return float(-np.mean(y * np.log(p) + (1 - y) * np.log(1 - p)))


def reliability(y: np.ndarray, p: np.ndarray, n_bins: int = 10) -> List[Dict[str, float]]:
    edges = np.linspace(0, 1, n_bins + 1)
    bins = np.clip(np.digitize(p, edges[1:-1]), 0, n_bins - 1)
    out = []
    for b in range(n_bins):
        mask = bins == b
        if mask.sum() == 0:
            continue
        out.append({
            "bin_lo": float(edges[b]),
            "bin_hi": float(edges[b + 1]),
            "n": int(mask.sum()),
            "mean_predicted": float(p[mask].mean()),
            "mean_realized": float(y[mask].mean()),
        })
    return out


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--training-table", type=Path, default=DEFAULT_TABLE)
    p.add_argument("--features", type=Path, default=DEFAULT_FEATURES)
    p.add_argument("--game-log", type=Path, default=DEFAULT_GAME_LOG)
    p.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    p.add_argument("--log-level", default="INFO")
    args = p.parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    LOGGER.info("Loading features")
    features = load_features(args.features)
    mlb_avg = load_mlb_avg(args.game_log)
    LOGGER.info("  mlb_avg_rpg=%.3f, %d (team,date) feature rows", mlb_avg, len(features))

    # Required raw feature keys for all models combined.
    required = ["rolling_rpg_50", "prior_season_rpg", "season_rpg_to_date", "momentum_rpg_10"]
    LOGGER.info("Joining calibration table to features")
    data = load_table_join_features(args.training_table, features, required)
    LOGGER.info("  joined rows: %d", len(data["y"]))

    # Apply EB shrinkage for windows we'll use ----------------------------
    LOGGER.info("Estimating sigma_within^2 (single-game variance) on training years")
    sigma_sq = estimate_within_variance(args.training_table, features, mlb_avg, TRAIN_SEASONS)
    LOGGER.info("  sigma_within^2 = %.3f (sigma = %.3f)", sigma_sq, math.sqrt(sigma_sq))

    # Effective n per window
    eff_n = {
        "rolling_rpg_50": 50,
        "season_rpg_to_date": 70,   # rough mid-season
        "prior_season_rpg": 162,
        "momentum_rpg_10": 10,
    }
    tau_sq_window: Dict[str, float] = {}
    shrinkage_k_window: Dict[str, float] = {}
    LOGGER.info("Estimating tau^2 per window on training years")
    for k, n_w in eff_n.items():
        tau = estimate_tau_sq(features, TRAIN_SEASONS, k, n_w, sigma_sq)
        tau_sq_window[k] = tau
        shrinkage_k_window[k] = sigma_sq / max(1e-6, tau)
        LOGGER.info("  %-22s tau^2=%.3f  shrinkage_k(equivalent prior games)=%.1f", k, tau, sigma_sq / max(1e-6, tau))

    # Add shrunk versions to data arrays.
    for k, n_w in eff_n.items():
        for side in ("away", "home"):
            raw = data[f"{side}_{k}"]
            data[f"{side}_{k}_shrunk"] = shrink(raw, n_w, mlb_avg, sigma_sq, tau_sq_window[k])

    # Define key-mappings used by models 2-4
    sk_50 = {"away": "away_rolling_rpg_50_shrunk", "home": "home_rolling_rpg_50_shrunk"}
    sk_blend = {
        "prior":    {"away": "away_prior_season_rpg_shrunk",   "home": "home_prior_season_rpg_shrunk"},
        "season":   {"away": "away_season_rpg_to_date_shrunk", "home": "home_season_rpg_to_date_shrunk"},
        "momentum": {"away": "away_momentum_rpg_10_shrunk",    "home": "home_momentum_rpg_10_shrunk"},
    }

    # Split into train / val / test ---------------------------------------
    season = data["season"]
    train_mask = np.isin(season, list(TRAIN_SEASONS))
    val_mask   = np.isin(season, list(VAL_SEASONS))
    test_mask  = np.isin(season, list(TEST_SEASONS))

    def slice_data(mask: np.ndarray) -> Dict[str, np.ndarray]:
        return {k: v[mask] for k, v in data.items() if isinstance(v, np.ndarray)}

    train = slice_data(train_mask)
    val   = slice_data(val_mask)
    test  = slice_data(test_mask)
    LOGGER.info("Splits: train=%d val=%d test=%d", len(train["y"]), len(val["y"]), len(test["y"]))

    # Fit each model on TRAIN ---------------------------------------------
    LOGGER.info("Fitting Model 1 (refit constant on v1 shape)")
    m1 = fit_model_1(train, mlb_avg)
    LOGGER.info("  beta_diff = %.4f (vs v1 = %.4f)", m1["beta_diff"], V1_LOGIT_DELTA_PER_RUN)

    LOGGER.info("Fitting Model 2 (EB-shrunk rolling_50, no clamp)")
    m2 = fit_model_2(train, mlb_avg, sk_50)
    LOGGER.info("  beta_diff = %.4f", m2["beta_diff"])

    LOGGER.info("Fitting Model 3 (blend prior/season/momentum, EB-shrunk)")
    m3 = fit_model_3(train, mlb_avg, sk_blend)
    LOGGER.info("  beta_prior=%.4f beta_season=%.4f beta_momentum=%.4f",
                m3["beta_prior"], m3["beta_season"], m3["beta_momentum"])

    LOGGER.info("Fitting Model 4 (Model 3 + per-inning weights)")
    m4 = fit_model_4(train, mlb_avg, sk_blend)
    LOGGER.info("  betas: prior=%.4f season=%.4f momentum=%.4f",
                m4["beta_prior"], m4["beta_season"], m4["beta_momentum"])
    LOGGER.info("  inning weights: %s", [round(x, 3) for x in m4["inning_weights"]])

    # Score each on VAL and TEST -----------------------------------------
    def score_all(d: Dict[str, np.ndarray], split_name: str) -> Dict[str, Dict[str, Any]]:
        out = {}
        scenarios = [
            ("baseline_A_no_stage3", predict_baselineA(d)),
            ("baseline_B_v1",        predict_baselineB(d, mlb_avg)),
            ("model_1_refit_const",  predict_model_1(d, m1, mlb_avg)),
            ("model_2_eb_shrunk",    predict_model_2(d, m2, mlb_avg, sk_50)),
            ("model_3_blend",        predict_model_3(d, m3, mlb_avg, sk_blend)),
            ("model_4_blend_per_inn", predict_model_4(d, m4, mlb_avg, sk_blend)),
        ]
        for name, p in scenarios:
            br = brier(d["y"], p)
            ll = log_loss(d["y"], p)
            out[name] = {
                "n": int(len(p)),
                "split": split_name,
                "brier": round(br, 6),
                "log_loss": round(ll, 6),
            }
        return out

    val_scores  = score_all(val,  "val_2025")
    test_scores = score_all(test, "test_2026")

    # Brier improvements vs Baseline A and Baseline B
    base_a_val = val_scores["baseline_A_no_stage3"]["brier"]
    base_b_val = val_scores["baseline_B_v1"]["brier"]
    base_a_test = test_scores["baseline_A_no_stage3"]["brier"]
    base_b_test = test_scores["baseline_B_v1"]["brier"]
    LOGGER.info("Brier on VAL  (baseline_A=%.6f, baseline_B=%.6f)", base_a_val, base_b_val)
    LOGGER.info("Brier on TEST (baseline_A=%.6f, baseline_B=%.6f)", base_a_test, base_b_test)

    # Reliability per model on test
    rel_test: Dict[str, List[Dict[str, float]]] = {}
    for name in ["baseline_A_no_stage3", "baseline_B_v1", "model_1_refit_const",
                 "model_2_eb_shrunk", "model_3_blend", "model_4_blend_per_inn"]:
        if name == "baseline_A_no_stage3":
            p = predict_baselineA(test)
        elif name == "baseline_B_v1":
            p = predict_baselineB(test, mlb_avg)
        elif name == "model_1_refit_const":
            p = predict_model_1(test, m1, mlb_avg)
        elif name == "model_2_eb_shrunk":
            p = predict_model_2(test, m2, mlb_avg, sk_50)
        elif name == "model_3_blend":
            p = predict_model_3(test, m3, mlb_avg, sk_blend)
        else:
            p = predict_model_4(test, m4, mlb_avg, sk_blend)
        rel_test[name] = reliability(test["y"], p)

    # Persist  -----------------------------------------------------------
    args.output_dir.mkdir(parents=True, exist_ok=True)
    out_json = args.output_dir / "phase4_models.json"
    payload = {
        "schema_version": 1,
        "mlb_avg_rpg": round(mlb_avg, 4),
        "splits": {
            "train_seasons": list(TRAIN_SEASONS),
            "val_seasons": list(VAL_SEASONS),
            "test_seasons": list(TEST_SEASONS),
            "n_train": int(train_mask.sum()),
            "n_val": int(val_mask.sum()),
            "n_test": int(test_mask.sum()),
        },
        "shrinkage": {
            "sigma_within_sq": round(sigma_sq, 4),
            "tau_sq_per_window": {k: round(v, 4) for k, v in tau_sq_window.items()},
            "shrinkage_k_per_window": {k: round(v, 2) for k, v in shrinkage_k_window.items()},
            "effective_n_per_window": eff_n,
        },
        "models": {
            "baseline_B_v1": {
                "logit_delta_per_run": V1_LOGIT_DELTA_PER_RUN,
                "max_logit_delta": V1_MAX_LOGIT_DELTA,
                "rpg_clamp_low_ratio": V1_RPG_CLAMP_LOW_RATIO,
                "rpg_clamp_high_ratio": V1_RPG_CLAMP_HIGH_RATIO,
                "inning_weight": "linear (9-inn)/8",
            },
            "model_1_refit_const": m1,
            "model_2_eb_shrunk": m2,
            "model_3_blend": m3,
            "model_4_blend_per_inn": m4,
        },
        "scores": {
            "val_2025": val_scores,
            "test_2026": test_scores,
        },
        "test_reliability": rel_test,
    }
    with open(out_json, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    LOGGER.info("Wrote %s", out_json)

    # Console summary -----------------------------------------------------
    print()
    print("=== MODEL FITS (training years 2021-2024) ===")
    print(f"  baseline B (v1):                 logit_delta_per_run = {V1_LOGIT_DELTA_PER_RUN:.4f}")
    print(f"  model 1 (refit constant):        beta_diff = {m1['beta_diff']:+.4f}  (ratio to v1: {m1['beta_diff']/V1_LOGIT_DELTA_PER_RUN:.2f}x)")
    print(f"  model 2 (EB-shrunk rolling_50):  beta_diff = {m2['beta_diff']:+.4f}")
    print(f"  model 3 (blend):                 b_prior={m3['beta_prior']:+.4f}  b_season={m3['beta_season']:+.4f}  b_momentum={m3['beta_momentum']:+.4f}")
    print(f"  model 4 (blend + per-inn):       b_prior={m4['beta_prior']:+.4f}  b_season={m4['beta_season']:+.4f}  b_momentum={m4['beta_momentum']:+.4f}")
    print(f"      inning weights: {[round(x,3) for x in m4['inning_weights']]}")

    print()
    print("=== SCORES (Brier  /  log-loss) ===")
    print(f"  {'model':<26} {'VAL_brier':>10} {'VAL_logloss':>11}   {'TEST_brier':>11} {'TEST_logloss':>12}   {'TEST vs A':>10} {'TEST vs B':>10}")
    for name in ["baseline_A_no_stage3", "baseline_B_v1", "model_1_refit_const",
                 "model_2_eb_shrunk", "model_3_blend", "model_4_blend_per_inn"]:
        v = val_scores[name]
        t = test_scores[name]
        rel_a = (base_a_test - t["brier"]) / base_a_test * 100
        rel_b = (base_b_test - t["brier"]) / base_b_test * 100
        print(f"  {name:<26} {v['brier']:>10.6f} {v['log_loss']:>11.6f}   "
              f"{t['brier']:>11.6f} {t['log_loss']:>12.6f}   "
              f"{rel_a:>+9.3f}% {rel_b:>+9.3f}%")


if __name__ == "__main__":
    main()
