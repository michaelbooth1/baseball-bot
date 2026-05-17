#!/usr/bin/env python3
"""build_under_candidate_universe.py -- Synthesize UNDER candidate rows
from existing OVER candidate-universe files.

Phase B prerequisite (A5, 2026-05-16). Pure offline / additive: this
reads the OVER candidate-universe rows the live engine already
writes, and writes a sibling per-date `<date>_under_candidates.jsonl`
file with UNDER-side projections of each OVER candidate that meets the
emission criteria.

Why offline synthesis instead of live-engine emission:
  - Path of lowest risk to the live trading runtime. The OVER
    candidate row already carries every game-state field needed
    (inning, runs, regime, etc.) plus under_best_bid/ask when
    under_pair_available=True. We can derive every UNDER candidate
    field without changing the live signal pipeline.
  - Same precedent as Phase A3 (UNDER state-value report) + A4
    (UNDER walk-forward), both of which flip OVER outputs offline.
  - Defers the live-engine change to Phase C, where two-sided
    quoting needs it for real-time UNDER decisions.

Synthesis rule: for each OVER candidate row with
`under_pair_available=True` AND `fair_value_raw is not None`, emit
an UNDER sibling with:
  - side: "under"
  - decision_ask: under_best_ask
  - best_bid: under_best_bid
  - spread: under_spread
  - fair_value_raw: 1 - over_fair_value_raw
  - fair_value_calibrated: under_calibrator(1 - over_fair_value_raw)
    when the UNDER calibrator artifact is loaded, else
    1 - over_fair_value_calibrated (fallback that at least preserves
    any Over-side calibration adjustment).
  - fair_value_calibration_*: same family + method as OVER, but with
    a `calibration_side="under"` marker so consumers know the curve
    came from the UNDER calibrator (not the OVER calibrator with a
    naive flip).
  - edge: under_fair_value_calibrated - decision_ask
  - decision: "shadow_under" (never trades; Phase A/B is offline)
  - decision_reason: "shadow_under_emission"
  - candidate_id: f"{over_id}__under" (deterministic sibling id)

Outputs:
  data/live_trading/candidate_universe/<date>_under_candidates.jsonl
  data/paper_trading/candidate_universe/<date>_under_candidates.jsonl
  data/analysis_output/under_candidate_universe/manifest.json
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional


PROJECT_DIR = Path(__file__).resolve().parents[2]
LIVE_ROOT = PROJECT_DIR / "data" / "live_trading" / "candidate_universe"
PAPER_ROOT = PROJECT_DIR / "data" / "paper_trading" / "candidate_universe"
DEFAULT_OUTPUT_MANIFEST = (
    PROJECT_DIR / "data" / "analysis_output" / "under_candidate_universe"
    / "manifest.json"
)
DEFAULT_UNDER_CALIBRATOR_PATH = (
    PROJECT_DIR / "data" / "analysis_output" / "calibration"
    / "signal_win_calibration_under.json"
)


LOGGER = logging.getLogger("build_under_candidate_universe")


def _safe_float(v: Any) -> Optional[float]:
    try:
        if v is None or v == "":
            return None
        f = float(v)
        if math.isnan(f) or math.isinf(f):
            return None
        return f
    except (TypeError, ValueError):
        return None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _iter_jsonl(path: Path) -> Iterable[Dict[str, Any]]:
    with open(path, encoding="utf-8") as f:
        for raw in f:
            raw = raw.strip()
            if not raw:
                continue
            try:
                row = json.loads(raw)
            except Exception:
                continue
            if isinstance(row, dict):
                yield row


def _clip_prob(p: float) -> float:
    return min(max(p, 1e-8), 1.0 - 1e-8)


def _stable_sigmoid(x: float) -> float:
    if x >= 0:
        z = math.exp(-x)
        return 1.0 / (1.0 + z)
    z = math.exp(x)
    return z / (1.0 + z)


def _logit(p: float) -> float:
    cp = _clip_prob(p)
    return math.log(cp / (1.0 - cp))


def _platt_apply(p: float, a: float, b: float) -> float:
    """Apply Platt scaling y = sigmoid(a + b * logit(p))."""
    return _stable_sigmoid(a + b * _logit(p))


def _isotonic_apply(p: float, knots: List[List[float]]) -> float:
    """Apply piecewise-linear isotonic interpolation.

    `knots` is a list of [x, y] pairs sorted by x. p is clipped to
    [min_x, max_x] then linearly interpolated.
    """
    if not knots:
        return p
    xs = [float(k[0]) for k in knots]
    ys = [float(k[1]) for k in knots]
    if p <= xs[0]:
        return ys[0]
    if p >= xs[-1]:
        return ys[-1]
    # Binary search insertion point
    lo, hi = 0, len(xs) - 1
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if xs[mid] <= p:
            lo = mid
        else:
            hi = mid
    x0, x1 = xs[lo], xs[hi]
    y0, y1 = ys[lo], ys[hi]
    if x1 == x0:
        return y0
    return y0 + (y1 - y0) * (p - x0) / (x1 - x0)


def load_under_calibrator(path: Path) -> Optional[Dict[str, Any]]:
    """Load the UNDER calibrator artifact built by
    `calibrate_signal_probabilities.py --side under`. Returns the
    payload dict so callers can route per-family. Returns None when
    the artifact is missing (first run before any UNDER refresh).
    """
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        LOGGER.warning("Failed to load UNDER calibrator %s: %s", path, exc)
        return None


def calibrate_under(
    raw_under: float, family: str, calibrator: Optional[Dict[str, Any]]
) -> Dict[str, Any]:
    """Apply the UNDER calibrator to (1 - over_raw).

    Returns a dict with:
      - fair_value_calibrated: float (or raw_under when no calibrator)
      - calibration_method: str ("platt"|"isotonic"|"identity"|"fallback_flip")
      - calibration_family: str
      - calibration_side: "under"
      - calibration_applied: bool

    `fallback_flip` is the safe fallback: when no UNDER artifact is
    present, downstream callers use (1 - over_fair_value_calibrated)
    as the UNDER calibrated value. This function does NOT compute
    that fallback -- it only signals it -- because the caller has
    access to the OVER fair_value_calibrated field.
    """
    if calibrator is None:
        return {
            "fair_value_calibrated": raw_under,
            "calibration_method": "fallback_flip",
            "calibration_family": family,
            "calibration_side": "under",
            "calibration_applied": False,
        }
    families = calibrator.get("families") or {}
    fam_payload = families.get(family)
    if not isinstance(fam_payload, dict):
        # Family not present in artifact (e.g. only one family had
        # enough rows). Fall back to identity, mark not-applied.
        return {
            "fair_value_calibrated": raw_under,
            "calibration_method": "identity",
            "calibration_family": family,
            "calibration_side": "under",
            "calibration_applied": False,
        }
    method = str(fam_payload.get("selected_method") or "identity")
    methods = fam_payload.get("methods") or {}
    if method == "platt":
        params = ((methods.get("platt") or {}).get("params") or {})
        a = _safe_float(params.get("a"))
        b = _safe_float(params.get("b"))
        if a is None or b is None:
            return {
                "fair_value_calibrated": raw_under,
                "calibration_method": "identity",
                "calibration_family": family,
                "calibration_side": "under",
                "calibration_applied": False,
            }
        return {
            "fair_value_calibrated": _platt_apply(raw_under, a, b),
            "calibration_method": "platt",
            "calibration_family": family,
            "calibration_side": "under",
            "calibration_applied": True,
        }
    if method == "isotonic":
        params = ((methods.get("isotonic") or {}).get("params") or {})
        knots = params.get("knots") or []
        if not knots:
            return {
                "fair_value_calibrated": raw_under,
                "calibration_method": "identity",
                "calibration_family": family,
                "calibration_side": "under",
                "calibration_applied": False,
            }
        return {
            "fair_value_calibrated": _isotonic_apply(raw_under, list(knots)),
            "calibration_method": "isotonic",
            "calibration_family": family,
            "calibration_side": "under",
            "calibration_applied": True,
        }
    # identity / unknown -> passthrough
    return {
        "fair_value_calibrated": raw_under,
        "calibration_method": "identity",
        "calibration_family": family,
        "calibration_side": "under",
        "calibration_applied": False,
    }


def synthesize_under_row(
    over_row: Dict[str, Any],
    *,
    under_calibrator: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """Return an UNDER candidate row synthesized from one OVER row,
    or None if the row doesn't meet the emission criteria.

    Criteria:
      - under_pair_available is True
      - fair_value_raw is present and in (0, 1)
      - under_best_ask is present and in (0, 1)
    """
    if not bool(over_row.get("under_pair_available")):
        return None
    over_raw = _safe_float(over_row.get("fair_value_raw"))
    if over_raw is None or not (0.0 < over_raw < 1.0):
        return None
    under_ask = _safe_float(over_row.get("under_best_ask"))
    if under_ask is None or not (0.0 < under_ask < 1.0):
        return None

    under_bid = _safe_float(over_row.get("under_best_bid"))
    under_spread = (
        round(under_ask - under_bid, 4)
        if under_bid is not None else None
    )

    raw_under = 1.0 - over_raw
    family = str(
        over_row.get("signal_model_family") or "score_event_transition"
    )
    calib = calibrate_under(raw_under, family, under_calibrator)
    fv_under_calibrated = float(calib["fair_value_calibrated"])
    if calib["calibration_method"] == "fallback_flip":
        # Fall back to 1 - over_fair_value_calibrated when OVER's
        # calibrated value is known. This preserves any OVER-side
        # calibration adjustment in the UNDER projection.
        over_calibrated = _safe_float(over_row.get("fair_value_calibrated"))
        if over_calibrated is not None and 0.0 < over_calibrated < 1.0:
            fv_under_calibrated = 1.0 - over_calibrated
            calib["fair_value_calibrated"] = fv_under_calibrated

    over_id = str(over_row.get("candidate_id") or "")
    under_id = f"{over_id}__under" if over_id else None

    # Clone every field from the OVER row, then override the
    # under-specific ones. Game-state context (inning, scores,
    # regime, weather, stage1/2/3 support diagnostics) carries
    # over unchanged.
    out = dict(over_row)
    out["side"] = "under"
    out["candidate_id"] = under_id
    out["bet_id"] = None  # never traded
    out["decision_ask"] = under_ask
    out["best_bid"] = under_bid
    out["spread"] = under_spread
    out["fair_value_raw"] = raw_under
    out["fair_value_calibrated"] = fv_under_calibrated
    # `fair_value` is the "final adjusted FV used for the trade
    # decision." For UNDER it is the calibrated value (or raw when
    # no calibrator). Downstream consumers that look at `fair_value`
    # get the most accurate UNDER estimate available.
    out["fair_value"] = fv_under_calibrated
    out["edge"] = round(fv_under_calibrated - under_ask, 6)
    out["fair_value_calibration_delta"] = (
        round(fv_under_calibrated - raw_under, 6)
    )
    out["fair_value_calibration_method"] = calib["calibration_method"]
    out["fair_value_calibration_family"] = calib["calibration_family"]
    out["fair_value_calibration_applied"] = calib["calibration_applied"]
    out["fair_value_calibration_side"] = "under"
    out["decision"] = "shadow_under"
    out["decision_reason"] = "shadow_under_emission"
    # Phase B foundation marker so downstream consumers can filter on it.
    out["under_synthesis_schema_version"] = 1
    out["under_synthesis_at_utc"] = _now_iso()
    # OVER-side context preserved so audits can trace back to source
    # without needing a join.
    out["over_source_candidate_id"] = over_id or None
    out["over_source_decision"] = over_row.get("decision")
    out["over_source_fair_value_calibrated"] = over_row.get(
        "fair_value_calibrated"
    )
    out["over_source_decision_ask"] = over_row.get("decision_ask")
    out["over_source_edge"] = over_row.get("edge")
    return out


def synthesize_for_path(
    over_path: Path,
    *,
    under_calibrator: Optional[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for row in _iter_jsonl(over_path):
        synth = synthesize_under_row(row, under_calibrator=under_calibrator)
        if synth is not None:
            out.append(synth)
    return out


def _write_jsonl(path: Path, rows: Iterable[Dict[str, Any]]) -> int:
    n = 0
    with open(path, "w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row) + "\n")
            n += 1
    return n


def _filter_path(
    path: Path, min_date: str, max_date: str
) -> bool:
    name = path.name
    date_str = name[:10]
    if min_date and date_str < min_date:
        return False
    if max_date and date_str > max_date:
        return False
    return True


def build_for_mode(
    over_root: Path,
    *,
    min_date: str,
    max_date: str,
    under_calibrator: Optional[Dict[str, Any]],
    write: bool = True,
) -> Dict[str, Any]:
    """Synthesize UNDER candidate files for one mode (live or paper).

    Reads each `<date>_candidates.jsonl` in `over_root`, writes
    `<date>_under_candidates.jsonl` alongside it. Returns a
    manifest dict listing input/output paths + row counts.
    """
    manifest: Dict[str, Any] = {
        "over_root": str(over_root),
        "files": [],
        "total_over_rows": 0,
        "total_under_rows": 0,
    }
    if not over_root.exists():
        return manifest

    for over_path in sorted(over_root.glob("*_candidates.jsonl")):
        if "_under_candidates" in over_path.name:
            continue
        if not _filter_path(over_path, min_date, max_date):
            continue
        date_str = over_path.name[:10]
        n_over_rows = sum(1 for _ in _iter_jsonl(over_path))
        under_rows = synthesize_for_path(
            over_path, under_calibrator=under_calibrator
        )
        out_path = over_path.parent / f"{date_str}_under_candidates.jsonl"
        if write:
            n_written = _write_jsonl(out_path, under_rows)
        else:
            n_written = len(under_rows)
        manifest["files"].append({
            "date": date_str,
            "over_path": str(over_path),
            "out_path": str(out_path),
            "n_over_rows": n_over_rows,
            "n_under_rows": n_written,
            "emission_rate": (
                round(n_written / n_over_rows, 4)
                if n_over_rows else None
            ),
        })
        manifest["total_over_rows"] += n_over_rows
        manifest["total_under_rows"] += n_written
    return manifest


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Synthesize UNDER candidate-universe rows from OVER."
    )
    p.add_argument(
        "--mode", choices=["live", "paper", "both"], default="both",
    )
    p.add_argument("--min-date", type=str, default="")
    p.add_argument("--max-date", type=str, default="")
    p.add_argument(
        "--under-calibrator-path", type=Path,
        default=DEFAULT_UNDER_CALIBRATOR_PATH,
    )
    p.add_argument(
        "--manifest-path", type=Path,
        default=DEFAULT_OUTPUT_MANIFEST,
    )
    return p.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> None:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s %(message)s",
    )
    if args.min_date:
        datetime.strptime(args.min_date, "%Y-%m-%d")
    if args.max_date:
        datetime.strptime(args.max_date, "%Y-%m-%d")
    if args.min_date and args.max_date and args.min_date > args.max_date:
        raise SystemExit("--min-date must be <= --max-date")

    under_calibrator = load_under_calibrator(args.under_calibrator_path)
    if under_calibrator is None:
        LOGGER.info(
            "UNDER calibrator artifact not present at %s; will use "
            "fallback_flip (1 - over_fair_value_calibrated) where the "
            "OVER calibrated value is known.",
            args.under_calibrator_path,
        )

    payload: Dict[str, Any] = {
        "generated_at_utc": _now_iso(),
        "schema_version": 1,
        "under_calibrator_path": str(args.under_calibrator_path),
        "under_calibrator_loaded": under_calibrator is not None,
        "config": {
            "mode": args.mode,
            "min_date": args.min_date or None,
            "max_date": args.max_date or None,
        },
        "modes": {},
    }
    if args.mode in ("live", "both"):
        payload["modes"]["live"] = build_for_mode(
            LIVE_ROOT, min_date=args.min_date, max_date=args.max_date,
            under_calibrator=under_calibrator,
        )
    if args.mode in ("paper", "both"):
        payload["modes"]["paper"] = build_for_mode(
            PAPER_ROOT, min_date=args.min_date, max_date=args.max_date,
            under_calibrator=under_calibrator,
        )

    args.manifest_path.parent.mkdir(parents=True, exist_ok=True)
    args.manifest_path.write_text(
        json.dumps(payload, indent=2), encoding="utf-8"
    )
    total_under = sum(
        m.get("total_under_rows", 0) for m in payload["modes"].values()
    )
    total_over = sum(
        m.get("total_over_rows", 0) for m in payload["modes"].values()
    )
    LOGGER.info(
        "Synthesized %d UNDER rows from %d OVER rows (rate=%s); "
        "manifest at %s",
        total_under, total_over,
        round(total_under / total_over, 4) if total_over else None,
        args.manifest_path,
    )


if __name__ == "__main__":
    main()
