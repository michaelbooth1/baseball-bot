#!/usr/bin/env python3
"""promote_team_offense_v2.py -- Promote a Stage-3 v2 fit to production.

Reads the latest research fit at
``data/analysis_output/team_offense_calibration/phase4_models.json`` and
writes a production-consumed weights JSON at
``cache/team_offense_v2_weights.json``. The runtime ``TeamOffenseModel.load()``
reads this file when present, overriding the compiled-in defaults.

This is a deliberate manual step: the daily refresh fits new weights every
day to the research path, but auto-promoting them into production without
a human-in-the-loop comparison risks silently swapping the live FV model.
Run this only after reviewing the fit (Brier improvement, sub-window
stability, Phase 5 dollar test).

Usage:
    python scripts/analysis/promote_team_offense_v2.py
    python scripts/analysis/promote_team_offense_v2.py --dry-run
    python scripts/analysis/promote_team_offense_v2.py --source-artifact path/to/phase4_models.json

A dry run prints what would be written without touching disk. The schema
matches `_load_weights_overrides` in `team_offense_model.py`.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict


PROJECT_DIR = Path(__file__).resolve().parents[2]
DEFAULT_SOURCE = (
    PROJECT_DIR
    / "data"
    / "analysis_output"
    / "team_offense_calibration"
    / "phase4_models.json"
)
DEFAULT_OUTPUT = PROJECT_DIR / "cache" / "team_offense_v2_weights.json"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _pick_model3_fit(phase4_payload: Dict[str, Any]) -> Dict[str, Any]:
    """Phase 4's JSON output stores fits keyed by model name; we want the
    Model 3 fit (the production design). Return its full-window fit
    section.

    The exact schema can drift -- we look for any of a few likely keys.
    """
    candidates = phase4_payload.get("models") or {}
    if not isinstance(candidates, dict):
        raise RuntimeError(
            "phase4_models.json: expected top-level 'models' dict, found "
            f"{type(candidates).__name__}"
        )
    for key in ("model_3_blend", "model_3", "Model 3", "model3", "phase4_model3"):
        if key in candidates:
            return candidates[key]
    # Fall back: pick any model whose summary mentions a three-window
    # blend so we don't silently promote the wrong fit.
    raise RuntimeError(
        "phase4_models.json: could not locate Model 3 fit. "
        f"Available models: {sorted(candidates.keys())}"
    )


def _extract_betas(model3: Dict[str, Any]) -> Dict[str, float]:
    """Pull the three betas out of the Model 3 fit object."""
    # Phase 4 output schema variations: try several known key paths.
    coefs = model3.get("coefficients") or model3.get("betas") or model3.get("fit") or model3
    if not isinstance(coefs, dict):
        raise RuntimeError(
            "Model 3 fit: no 'coefficients'/'betas'/'fit' dict found"
        )
    out: Dict[str, float] = {}
    for src_key, dest_key in (
        ("prior_season", "prior_season"),
        ("beta_prior_season", "prior_season"),
        ("beta_prior", "prior_season"),
        ("b_prior", "prior_season"),
        ("season_to_date", "season_to_date"),
        ("beta_season_to_date", "season_to_date"),
        ("beta_season", "season_to_date"),
        ("b_season", "season_to_date"),
        ("momentum_10", "momentum_10"),
        ("beta_momentum_10", "momentum_10"),
        ("beta_momentum", "momentum_10"),
        ("b_momentum", "momentum_10"),
    ):
        if src_key in coefs:
            out[dest_key] = float(coefs[src_key])
    missing = [k for k in ("prior_season", "season_to_date", "momentum_10") if k not in out]
    if missing:
        raise RuntimeError(
            f"Model 3 fit missing required betas: {missing}. Available keys: {sorted(coefs.keys())}"
        )
    return out


def _extract_shrinkage(phase4_payload: Dict[str, Any]) -> Dict[str, float]:
    """Pull the EB shrinkage params; fall back to compiled defaults if
    the artifact doesn't carry them (some older fits don't)."""
    candidates = phase4_payload.get("shrinkage") or phase4_payload.get("eb_params") or {}
    if not isinstance(candidates, dict):
        return {}
    out: Dict[str, float] = {}
    
    # 1. Check flat keys
    for src in ("sigma_within_sq", "tau_sq_season", "tau_sq_prior", "tau_sq_momentum"):
        if src in candidates:
            out[src] = float(candidates[src])
            
    # 2. Check nested keys
    if "sigma_within_sq" in candidates and "sigma_within_sq" not in out:
        out["sigma_within_sq"] = float(candidates["sigma_within_sq"])
        
    tau_sq_nested = candidates.get("tau_sq_per_window") or {}
    if isinstance(tau_sq_nested, dict):
        if "season_rpg_to_date" in tau_sq_nested:
            out["tau_sq_season"] = float(tau_sq_nested["season_rpg_to_date"])
        if "prior_season_rpg" in tau_sq_nested:
            out["tau_sq_prior"] = float(tau_sq_nested["prior_season_rpg"])
        if "momentum_rpg_10" in tau_sq_nested:
            out["tau_sq_momentum"] = float(tau_sq_nested["momentum_rpg_10"])
            
    return out


def build_weights_payload(source: Path) -> Dict[str, Any]:
    if not source.exists():
        raise RuntimeError(f"Source artifact not found: {source}")
    with open(source, encoding="utf-8") as f:
        phase4 = json.load(f)
    model3 = _pick_model3_fit(phase4)
    betas = _extract_betas(model3)
    shrinkage = _extract_shrinkage(phase4)
    payload: Dict[str, Any] = {
        "schema_version": 1,
        "generated_at_utc": _now_iso(),
        "source_artifact": str(source),
        "model_name": "phase4_model3",
        "betas": betas,
    }
    if shrinkage:
        payload["shrinkage"] = shrinkage
    # max_logit_delta is a production-side bound, not part of the fit.
    # Always emit the production default so the JSON is self-contained.
    payload["bounds"] = {"max_logit_delta": 0.60}
    return payload


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Promote a Stage-3 v2 fit to production.")
    p.add_argument("--source-artifact", type=Path, default=DEFAULT_SOURCE)
    p.add_argument("--output-path", type=Path, default=DEFAULT_OUTPUT)
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the would-be payload without writing to disk.",
    )
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    try:
        payload = build_weights_payload(args.source_artifact)
    except RuntimeError as exc:
        print(f"Promotion failed: {exc}", file=sys.stderr)
        return 1

    # Active #16 v2 (2026-05-17): stamp lineage on the promoted
    # weights JSON. Anchored on the source artifact's hash so when
    # the cache file is later inspected the operator can trace it
    # back to a specific phase4_models.json + the git_sha that ran
    # the promotion. Fail-open: lineage failure must not block the
    # promotion write.
    try:
        from scripts.analysis.artifact_lineage import compute_lineage as _compute_lineage
    except ImportError:
        try:
            from artifact_lineage import compute_lineage as _compute_lineage  # type: ignore[no-redef]
        except ImportError:
            _compute_lineage = None  # type: ignore[assignment]
    if _compute_lineage is not None:
        try:
            from pathlib import Path as _P
            _project_dir = _P(__file__).resolve().parents[2]
            payload["lineage"] = _compute_lineage(
                builder_path=__file__,
                input_paths=[args.source_artifact],
                project_root=_project_dir,
                extra={
                    "cli_args_summary": {
                        "source_artifact": str(args.source_artifact),
                        "output_path": str(args.output_path),
                        "dry_run": bool(args.dry_run),
                    },
                },
            )
        except Exception as _lineage_exc:  # noqa: BLE001
            print(
                f"[lineage] warning: stamp failed: {_lineage_exc!r}",
                file=sys.stderr,
            )

    rendered = json.dumps(payload, indent=2, sort_keys=True)
    if args.dry_run:
        print(rendered)
        return 0
    args.output_path.parent.mkdir(parents=True, exist_ok=True)
    args.output_path.write_text(rendered + "\n", encoding="utf-8")
    print(f"Wrote {args.output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
