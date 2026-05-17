#!/usr/bin/env python3
"""
team_offense_model.py -- Stage-3 team-offense runtime applier.

Deployed 2026-05-07 (TR20). Replaces the prior single-50-game-rolling-window
model (LOGIT_DELTA_PER_RUN=0.20 + hard clamp + linear inning weight) which
the V2 calibration work showed was ~3.2x too aggressive on a 1.13M-row
leakage-free residual table over 2021-2026.

Design (Model 3 from
`model_improvements/team_offense_v2_phase4_findings_2026_05_07.txt`):

  - Decomposed per-team prior into THREE windows:
        prior_season_rpg    (last full completed season; coef NEGATIVE)
        season_rpg_to_date  (current season, all prior games)
        momentum_rpg_10     (trailing 10 games)
  - Empirical-Bayes shrinkage on each window (replaces v1's hard
    `[0.55*mu, 1.55*mu]` clamp).
  - Three fitted coefficients applied as a linear blend on the
    (away_diff + home_diff) sum, then multiplied by the linear inning
    weight `max(0, (9-inning)/8)`.

Coefficients are the full-window 2021-2024 Model 3 fit. Sub-window
stability check (Phase 4.5) confirmed b_prior is negative in 8/8 sub-
windows, b_season positive in 8/8, b_momentum positive in 7/8.

  beta_prior   = -0.1514     <-- NEGATIVE (regression-to-mean correction)
  beta_season  = +0.1407
  beta_mom10   = +0.1503

Provenance:
  model_improvements/team_offense_v2_plan_2026_05_07.txt
  model_improvements/team_offense_v2_phase1_findings_2026_05_07.txt
  model_improvements/team_offense_v2_phase4_findings_2026_05_07.txt
  model_improvements/team_offense_v2_phase45_stability_2026_05_07.txt
  model_improvements/team_offense_v2_phase5_findings_2026_05_07.txt
  data/analysis_output/team_offense_calibration/phase4_models.json

Public surface (consumed by signal_engine, live_engine, analysis scripts):
  - .mlb_avg_rpg, .mlb_avg_total, .n_games (compatibility)
  - .load(game_log_path, auto_rebuild)
  - .adjust_fv(base_fv, away_abbrev, home_abbrev, game_date, inning)
  - .get_matchup_delta(away_abbrev, home_abbrev, game_date, inning)
  - .get_inning_weight(inning)
  - .feature_breakdown(...) -> dict of all component diffs and shrunk values
  - .describe(...)
"""

from __future__ import annotations

import bisect
import json
import logging
import math
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

LOGGER = logging.getLogger("team_offense_model")

# ---------------------------------------------------------------------------
# Production constants -- Phase 4 Model 3 full-window fit (2021-2024).
# DO NOT change these without re-running scripts/analysis/calibrate_team_offense_v2.py
# and verifying via Phase 4.5 stability + Phase 5 dollar test.
# ---------------------------------------------------------------------------

DEFAULT_BETA_PRIOR_SEASON: float = -0.1514
DEFAULT_BETA_SEASON_TO_DATE: float = +0.1407
DEFAULT_BETA_MOMENTUM_10: float = +0.1503

# EB shrinkage parameters (estimated from 2021-2024 training; sigma^2 from
# single-game variance, tau^2 per window from across-team variance).
DEFAULT_SIGMA_WITHIN_SQ: float = 10.001
DEFAULT_TAU_SQ_SEASON: float = 0.237
DEFAULT_TAU_SQ_PRIOR: float = 0.171
DEFAULT_TAU_SQ_MOMENTUM: float = 0.198

# Effective n per window (used in the shrinkage weight n / (n+k)).
DEFAULT_N_SEASON: int = 70
DEFAULT_N_PRIOR: int = 162
DEFAULT_N_MOMENTUM: int = 10

# Minimum support to use a window's value; below this, fall back to mu_league.
MIN_SUPPORT_SEASON: int = 5
MIN_SUPPORT_PRIOR: int = 30
MIN_SUPPORT_MOMENTUM: int = 5

# Bounded output.
DEFAULT_MAX_LOGIT_DELTA: float = 0.60

# Cache staleness: triggers rebuild via build_team_game_log.py.
CACHE_MAX_AGE_DAYS: int = 1

PROJECT_DIR = Path(__file__).resolve().parents[2]
DEFAULT_GAME_LOG_PATH = PROJECT_DIR / "cache" / "team_game_log.json"

# Optional external weights JSON (Stage-3 promotion path, 2026-05-12).
# When this file exists, `load()` reads the betas + shrinkage params from
# it; otherwise the compiled-in DEFAULT_BETA_* constants apply. This is the
# "research output -> production weights" boundary: the daily refresh fits
# `data/analysis_output/team_offense_calibration/phase4_models.json` every
# day, but production only consumes new weights after an explicit promotion
# via `scripts/analysis/promote_team_offense_v2.py` (which validates and
# copies the relevant fit into the JSON below). Auto-promotion would need
# a "new vs prod" comparison gate; until that lands, promotion is manual.
DEFAULT_WEIGHTS_PATH = PROJECT_DIR / "cache" / "team_offense_v2_weights.json"

EPS = 1e-6


# ---------------------------------------------------------------------------
# Math
# ---------------------------------------------------------------------------


def _clamp01(p: float) -> float:
    return max(EPS, min(1.0 - EPS, p))


def _logit(p: float) -> float:
    p = _clamp01(p)
    return math.log(p / (1.0 - p))


def _sigmoid(x: float) -> float:
    return 1.0 / (1.0 + math.exp(-x))


def _shrink(observed: Optional[float], n_window: int, sigma_sq: float, tau_sq: float, mu_league: float) -> Optional[float]:
    """EB-shrunk RPG estimate. Returns None if `observed` is None."""
    if observed is None:
        return None
    k = sigma_sq / max(EPS, tau_sq)
    w = n_window / (n_window + k)
    return w * observed + (1 - w) * mu_league


# ---------------------------------------------------------------------------
# Per-team windowed RPG (leakage-free)
# ---------------------------------------------------------------------------


@dataclass
class _TeamHistory:
    """Sorted-by-date list of (date, runs_scored). All dates strings YYYY-MM-DD."""
    entries: List[Tuple[str, int]]
    dates_only: List[str]  # parallel array of dates for bisect


def _bisect_strict_less(dates: List[str], target_date: str) -> int:
    """Index of first entry with date >= target_date (so dates[:idx] are all strictly before)."""
    return bisect.bisect_left(dates, target_date)


# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------


class TeamOffenseModel:
    """
    Stage-3 team-offense applier (TR20 Model 3 deployment, 2026-05-07).

    Coefficients and shrinkage parameters are baked into module constants
    (Phase 4 fit). Override via constructor for testing only.
    """

    def __init__(
        self,
        by_team: Dict[str, _TeamHistory],
        mlb_avg_rpg: float,
        beta_prior_season: float = DEFAULT_BETA_PRIOR_SEASON,
        beta_season_to_date: float = DEFAULT_BETA_SEASON_TO_DATE,
        beta_momentum_10: float = DEFAULT_BETA_MOMENTUM_10,
        sigma_within_sq: float = DEFAULT_SIGMA_WITHIN_SQ,
        tau_sq_season: float = DEFAULT_TAU_SQ_SEASON,
        tau_sq_prior: float = DEFAULT_TAU_SQ_PRIOR,
        tau_sq_momentum: float = DEFAULT_TAU_SQ_MOMENTUM,
        max_logit_delta: float = DEFAULT_MAX_LOGIT_DELTA,
    ):
        self._by_team = by_team
        self.mlb_avg_rpg = mlb_avg_rpg
        self.mlb_avg_total = 2.0 * mlb_avg_rpg
        # Compatibility: legacy callers log `n_games`. We don't have a single
        # window number; report the dominant momentum window for continuity.
        self.n_games = DEFAULT_N_MOMENTUM
        self.beta_prior_season = beta_prior_season
        self.beta_season_to_date = beta_season_to_date
        self.beta_momentum_10 = beta_momentum_10
        self.sigma_within_sq = sigma_within_sq
        self.tau_sq_season = tau_sq_season
        self.tau_sq_prior = tau_sq_prior
        self.tau_sq_momentum = tau_sq_momentum
        self.max_logit_delta = max_logit_delta

    # ------------------------------------------------------------------
    # Factory
    # ------------------------------------------------------------------

    @classmethod
    def load(
        cls,
        game_log_path: Path = DEFAULT_GAME_LOG_PATH,
        auto_rebuild: bool = True,
        weights_path: Path = DEFAULT_WEIGHTS_PATH,
    ) -> "TeamOffenseModel":
        """Load from `cache/team_game_log.json`, rebuilding if stale.

        If ``weights_path`` exists, betas + shrinkage params are loaded
        from it (overriding the compiled-in defaults). This is the
        promotion path: the daily refresh fits new weights to a research
        JSON every day, but production picks them up only after an
        explicit promotion writes ``team_offense_v2_weights.json``.
        """
        game_log_path = Path(game_log_path)
        need_build = False
        if not game_log_path.exists():
            LOGGER.info("Team game log not found at %s -- building now...", game_log_path)
            need_build = True
        else:
            age_days = (time.time() - game_log_path.stat().st_mtime) / 86400
            if age_days > CACHE_MAX_AGE_DAYS:
                LOGGER.info("Team game log is %.1f days old -- rebuilding...", age_days)
                need_build = True
        if need_build:
            if not auto_rebuild:
                raise FileNotFoundError(
                    f"Team game log not found: {game_log_path}. "
                    "Run scripts/analysis/build_team_game_log.py or set auto_rebuild=True."
                )
            cls._rebuild(game_log_path)
        with open(game_log_path, encoding="utf-8") as f:
            payload = json.load(f)
        overrides = _load_weights_overrides(Path(weights_path))
        return cls.from_payload(payload, **overrides)

    @classmethod
    def from_payload(cls, payload: dict, **overrides: Any) -> "TeamOffenseModel":
        by_team_raw: Dict[str, List[Tuple[str, int]]] = {}
        for g in payload.get("games", []):
            for abbrev, runs in [(g["away"], int(g["away_runs"])),
                                 (g["home"], int(g["home_runs"]))]:
                by_team_raw.setdefault(abbrev, []).append((g["date"], int(runs)))
        by_team: Dict[str, _TeamHistory] = {}
        for t, entries in by_team_raw.items():
            entries.sort(key=lambda x: x[0])
            by_team[t] = _TeamHistory(entries=entries, dates_only=[d for d, _ in entries])
        mlb_avg_rpg = float(payload.get("mlb_avg_rpg", 4.45))
        # If overrides came from an external weights JSON, log the
        # effective betas; otherwise log the compiled defaults.
        bp = overrides.get("beta_prior_season", DEFAULT_BETA_PRIOR_SEASON)
        bs = overrides.get("beta_season_to_date", DEFAULT_BETA_SEASON_TO_DATE)
        bm = overrides.get("beta_momentum_10", DEFAULT_BETA_MOMENTUM_10)
        source = "external_weights" if overrides else "compiled_defaults"
        LOGGER.info(
            "TeamOffenseModel loaded (TR20 Model 3, %s): %d teams, mlb_avg_rpg=%.3f  "
            "betas=(prior=%+.4f season=%+.4f mom10=%+.4f)",
            source, len(by_team), mlb_avg_rpg, bp, bs, bm,
        )
        return cls(by_team=by_team, mlb_avg_rpg=mlb_avg_rpg, **overrides)

    @staticmethod
    def _rebuild(output_path: Path) -> None:
        import subprocess, sys
        builder = Path(__file__).parent / "build_team_game_log.py"
        result = subprocess.run(
            [sys.executable, str(builder), "--output", str(output_path)],
            capture_output=False,
        )
        if result.returncode != 0:
            raise RuntimeError(f"build_team_game_log.py failed: {result.returncode}")

    # ------------------------------------------------------------------
    # Per-team windowed RPG
    # ------------------------------------------------------------------

    def _windowed_rpg(
        self,
        abbrev: str,
        before_date: str,
    ) -> Tuple[Optional[float], Optional[float], Optional[float]]:
        """
        Returns (prior_season_rpg, season_to_date_rpg, momentum_10_rpg).
        Each is None if support below the per-window minimum.
        All computed using games STRICTLY before `before_date`.
        """
        h = self._by_team.get(abbrev)
        if h is None:
            return None, None, None
        idx = _bisect_strict_less(h.dates_only, before_date)
        prior = h.entries[:idx]
        if not prior:
            return None, None, None

        target_year = before_date[:4]
        prior_year = str(int(target_year) - 1)

        season_runs = [r for d, r in prior if d[:4] == target_year]
        prior_season_runs = [r for d, r in prior if d[:4] == prior_year]
        last10 = prior[-10:]
        last10_runs = [r for _, r in last10]

        season_rpg = (
            sum(season_runs) / len(season_runs)
            if len(season_runs) >= MIN_SUPPORT_SEASON
            else None
        )
        prior_rpg = (
            sum(prior_season_runs) / len(prior_season_runs)
            if len(prior_season_runs) >= MIN_SUPPORT_PRIOR
            else None
        )
        mom10_rpg = (
            sum(last10_runs) / len(last10_runs)
            if len(last10_runs) >= MIN_SUPPORT_MOMENTUM
            else None
        )
        return prior_rpg, season_rpg, mom10_rpg

    # ------------------------------------------------------------------
    # Legacy diagnostic accessors (kept for back-compat with log lines
    # in signal_pipeline_gates_post_fv.py). Not used by the prediction
    # path. Return the EB-shrunk season_to_date RPG when available,
    # falling back to momentum_10, then prior_season, then mu_league.
    # ------------------------------------------------------------------

    def get_rpg(self, abbrev: str, before_date: str) -> float:
        prior_rpg, season_rpg, mom10_rpg = self._windowed_rpg(abbrev, before_date)
        for raw, n_w, tau_sq in (
            (season_rpg, DEFAULT_N_SEASON, self.tau_sq_season),
            (mom10_rpg,  DEFAULT_N_MOMENTUM, self.tau_sq_momentum),
            (prior_rpg,  DEFAULT_N_PRIOR, self.tau_sq_prior),
        ):
            shrunk = _shrink(raw, n_w, self.sigma_within_sq, tau_sq, self.mlb_avg_rpg)
            if shrunk is not None:
                return shrunk
        return self.mlb_avg_rpg

    def get_expected_total(self, away_abbrev: str, home_abbrev: str, game_date: str) -> float:
        return self.get_rpg(away_abbrev, game_date) + self.get_rpg(home_abbrev, game_date)

    # ------------------------------------------------------------------
    # Inning weight (linear ramp; per-inning scalars tested in Phase 4
    # but overfit on holdout, so kept linear).
    # ------------------------------------------------------------------

    def get_inning_weight(self, inning: int) -> float:
        return max(0.0, (9.0 - float(inning)) / 8.0)

    # ------------------------------------------------------------------
    # Core: matchup delta + adjusted FV
    # ------------------------------------------------------------------

    def get_matchup_delta(
        self,
        away_abbrev: str,
        home_abbrev: str,
        game_date: str,
        inning: int,
    ) -> float:
        """
        Logit-space delta for the matchup. Positive -> raise FV (Over more
        likely). Returns 0.0 if neither team has any usable feature.
        """
        away_prior, away_season, away_mom = self._windowed_rpg(away_abbrev, game_date)
        home_prior, home_season, home_mom = self._windowed_rpg(home_abbrev, game_date)

        def shrink_or_mu(v: Optional[float], n_w: int, tau_sq: float) -> float:
            shrunk = _shrink(v, n_w, self.sigma_within_sq, tau_sq, self.mlb_avg_rpg)
            return self.mlb_avg_rpg if shrunk is None else shrunk

        a_prior = shrink_or_mu(away_prior, DEFAULT_N_PRIOR, self.tau_sq_prior)
        h_prior = shrink_or_mu(home_prior, DEFAULT_N_PRIOR, self.tau_sq_prior)
        a_season = shrink_or_mu(away_season, DEFAULT_N_SEASON, self.tau_sq_season)
        h_season = shrink_or_mu(home_season, DEFAULT_N_SEASON, self.tau_sq_season)
        a_mom = shrink_or_mu(away_mom, DEFAULT_N_MOMENTUM, self.tau_sq_momentum)
        h_mom = shrink_or_mu(home_mom, DEFAULT_N_MOMENTUM, self.tau_sq_momentum)

        d_prior = (a_prior + h_prior) - 2.0 * self.mlb_avg_rpg
        d_season = (a_season + h_season) - 2.0 * self.mlb_avg_rpg
        d_mom = (a_mom + h_mom) - 2.0 * self.mlb_avg_rpg

        weight = self.get_inning_weight(inning)
        delta = (
            self.beta_prior_season * d_prior
            + self.beta_season_to_date * d_season
            + self.beta_momentum_10 * d_mom
        ) * weight
        if delta > self.max_logit_delta:
            delta = self.max_logit_delta
        elif delta < -self.max_logit_delta:
            delta = -self.max_logit_delta
        return delta

    def adjust_fv(
        self,
        base_fv: Optional[float],
        away_abbrev: str,
        home_abbrev: str,
        game_date: str,
        inning: int,
    ) -> Optional[float]:
        if base_fv is None:
            return base_fv
        delta = self.get_matchup_delta(away_abbrev, home_abbrev, game_date, inning)
        if abs(delta) < 0.01:
            return base_fv
        return _clamp01(_sigmoid(_logit(base_fv) + delta))

    # ------------------------------------------------------------------
    # Diagnostics
    # ------------------------------------------------------------------

    def feature_breakdown(
        self,
        away_abbrev: str,
        home_abbrev: str,
        game_date: str,
        inning: int,
    ) -> Dict[str, Any]:
        """Full attribution -- raw + shrunk values + per-window contribution to delta."""
        away_prior, away_season, away_mom = self._windowed_rpg(away_abbrev, game_date)
        home_prior, home_season, home_mom = self._windowed_rpg(home_abbrev, game_date)

        def shrink_or_mu(v, n_w, tau_sq):
            return _shrink(v, n_w, self.sigma_within_sq, tau_sq, self.mlb_avg_rpg) or self.mlb_avg_rpg

        a_prior = shrink_or_mu(away_prior, DEFAULT_N_PRIOR, self.tau_sq_prior)
        h_prior = shrink_or_mu(home_prior, DEFAULT_N_PRIOR, self.tau_sq_prior)
        a_season = shrink_or_mu(away_season, DEFAULT_N_SEASON, self.tau_sq_season)
        h_season = shrink_or_mu(home_season, DEFAULT_N_SEASON, self.tau_sq_season)
        a_mom = shrink_or_mu(away_mom, DEFAULT_N_MOMENTUM, self.tau_sq_momentum)
        h_mom = shrink_or_mu(home_mom, DEFAULT_N_MOMENTUM, self.tau_sq_momentum)

        d_prior = (a_prior + h_prior) - 2.0 * self.mlb_avg_rpg
        d_season = (a_season + h_season) - 2.0 * self.mlb_avg_rpg
        d_mom = (a_mom + h_mom) - 2.0 * self.mlb_avg_rpg
        weight = self.get_inning_weight(inning)

        contribs = {
            "prior_season":   self.beta_prior_season   * d_prior * weight,
            "season_to_date": self.beta_season_to_date * d_season * weight,
            "momentum_10":    self.beta_momentum_10    * d_mom * weight,
        }
        delta = sum(contribs.values())
        if delta > self.max_logit_delta:
            delta_capped = self.max_logit_delta
        elif delta < -self.max_logit_delta:
            delta_capped = -self.max_logit_delta
        else:
            delta_capped = delta

        return {
            "away_abbrev": away_abbrev,
            "home_abbrev": home_abbrev,
            "game_date": game_date,
            "inning": inning,
            "inning_weight": round(weight, 4),
            "raw": {
                "away_prior_season_rpg": away_prior,
                "home_prior_season_rpg": home_prior,
                "away_season_to_date_rpg": away_season,
                "home_season_to_date_rpg": home_season,
                "away_momentum_10_rpg": away_mom,
                "home_momentum_10_rpg": home_mom,
            },
            "shrunk": {
                "away_prior_season_rpg": round(a_prior, 4),
                "home_prior_season_rpg": round(h_prior, 4),
                "away_season_to_date_rpg": round(a_season, 4),
                "home_season_to_date_rpg": round(h_season, 4),
                "away_momentum_10_rpg": round(a_mom, 4),
                "home_momentum_10_rpg": round(h_mom, 4),
            },
            "diffs_vs_mlb_total": {
                "prior_season":   round(d_prior, 4),
                "season_to_date": round(d_season, 4),
                "momentum_10":    round(d_mom, 4),
            },
            "contribution_to_delta_logit": {k: round(v, 6) for k, v in contribs.items()},
            "delta_logit": round(delta_capped, 6),
            "delta_logit_uncapped": round(delta, 6),
            "mlb_avg_rpg": self.mlb_avg_rpg,
        }

    def describe(
        self,
        away_abbrev: str,
        home_abbrev: str,
        game_date: str,
        inning: int = 1,
        base_fv: float = 0.50,
    ) -> str:
        b = self.feature_breakdown(away_abbrev, home_abbrev, game_date, inning)
        adj = self.adjust_fv(base_fv, away_abbrev, home_abbrev, game_date, inning)
        contribs = b["contribution_to_delta_logit"]
        return (
            f"{away_abbrev}@{home_abbrev}  {game_date}  inn={inning}  weight={b['inning_weight']}\n"
            f"  prior_season  diff={b['diffs_vs_mlb_total']['prior_season']:+.3f}  "
            f"contrib={contribs['prior_season']:+.4f}\n"
            f"  season_to_date diff={b['diffs_vs_mlb_total']['season_to_date']:+.3f}  "
            f"contrib={contribs['season_to_date']:+.4f}\n"
            f"  momentum_10   diff={b['diffs_vs_mlb_total']['momentum_10']:+.3f}  "
            f"contrib={contribs['momentum_10']:+.4f}\n"
            f"  total_delta_logit={b['delta_logit']:+.4f}\n"
            f"  base_fv={base_fv:.3f}  adjusted_fv={adj:.3f}  change={adj - base_fv:+.4f}"
        )


# ---------------------------------------------------------------------------
# External weights JSON support (2026-05-12 promotion path)
# ---------------------------------------------------------------------------


WEIGHTS_JSON_SCHEMA_VERSION = 1


def _load_weights_overrides(weights_path: Path) -> Dict[str, Any]:
    """Read overrides from `cache/team_offense_v2_weights.json` if present.

    Returns a dict of kwargs accepted by ``TeamOffenseModel.__init__``,
    or ``{}`` when the file is absent (so compiled defaults apply).

    Schema (v1):
        {
          "schema_version": 1,
          "generated_at_utc": "2026-05-12T15:00:00Z",
          "source_artifact": "data/.../phase4_models.json",
          "model_name": "phase4_model3",
          "betas": {
            "prior_season": -0.1514,
            "season_to_date": 0.1407,
            "momentum_10": 0.1503
          },
          "shrinkage": {
            "sigma_within_sq": 10.001,
            "tau_sq_season": 0.237,
            "tau_sq_prior": 0.171,
            "tau_sq_momentum": 0.198
          },
          "bounds": { "max_logit_delta": 0.60 }
        }

    Missing fields fall back to compiled defaults; unknown fields are
    ignored. Schema-version mismatch logs a warning and uses defaults.
    """
    if not weights_path.exists():
        return {}
    try:
        with open(weights_path, encoding="utf-8") as f:
            payload = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        LOGGER.warning(
            "Failed to load Stage-3 weights JSON at %s (%s); "
            "falling back to compiled defaults.",
            weights_path, exc,
        )
        return {}

    schema_version = payload.get("schema_version")
    if schema_version != WEIGHTS_JSON_SCHEMA_VERSION:
        LOGGER.warning(
            "Stage-3 weights JSON schema_version=%r (expected %d); "
            "falling back to compiled defaults.",
            schema_version, WEIGHTS_JSON_SCHEMA_VERSION,
        )
        return {}

    overrides: Dict[str, Any] = {}
    betas = payload.get("betas") or {}
    if isinstance(betas, dict):
        if "prior_season" in betas:
            overrides["beta_prior_season"] = float(betas["prior_season"])
        if "season_to_date" in betas:
            overrides["beta_season_to_date"] = float(betas["season_to_date"])
        if "momentum_10" in betas:
            overrides["beta_momentum_10"] = float(betas["momentum_10"])

    shrinkage = payload.get("shrinkage") or {}
    if isinstance(shrinkage, dict):
        for key in ("sigma_within_sq", "tau_sq_season", "tau_sq_prior", "tau_sq_momentum"):
            if key in shrinkage:
                overrides[key] = float(shrinkage[key])

    bounds = payload.get("bounds") or {}
    if isinstance(bounds, dict) and "max_logit_delta" in bounds:
        overrides["max_logit_delta"] = float(bounds["max_logit_delta"])

    return overrides


# ---------------------------------------------------------------------------
# CLI quick-inspect
# ---------------------------------------------------------------------------


def main() -> None:
    import argparse
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    p = argparse.ArgumentParser(description="Inspect Stage-3 adjustments.")
    p.add_argument("--away", required=True)
    p.add_argument("--home", required=True)
    p.add_argument("--date", required=True)
    p.add_argument("--inning", type=int, default=1)
    p.add_argument("--base-fv", type=float, default=0.50)
    p.add_argument("--game-log", type=Path, default=DEFAULT_GAME_LOG_PATH)
    args = p.parse_args()

    model = TeamOffenseModel.load(args.game_log)
    print(model.describe(args.away, args.home, args.date, args.inning, args.base_fv))


if __name__ == "__main__":
    main()
