#!/usr/bin/env python3
"""
build_stage1_nb_replay_report.py -- out-of-sample Stage-1 cache replay.

Hygiene #3 follow-up (2026-06-11). The NB staging cache's acceptance
check (93.4% of the +10.7pp high-FV poisson-vs-empirical gap closed)
is IN-CORPUS: the NB dispersion is fit on the same 2021-2025 games the
empirical rates come from. This report is the out-of-sample test that
doesn't wait two weeks for the O_nb_stage1 fleet arm: every settled
2026 candidate/bet row carries its Stage-1 cell key, line key, and the
logit-additive FV chain inputs -- and NO 2026 game is in the cache's
training window. Replaying alternative caches against those rows
measures realized calibration on exactly the signals the bot fired.

Method (delta substitution): for each settled row,
    delta      = logit(candidate_cache_po) - logit(prod_cache_po)
    p0_variant = sigmoid(logit(base_fair_value) + delta)
    p3_variant = sigmoid(logit(p0_variant) + s2_delta + s3_delta)
i.e. the candidate cache's smoothing delta is applied ON TOP of
whatever production's base was (including fallback penalties and
line-fallback calibration), isolating the smoothing change while
preserving the rest of the runtime chain. p3 here is the RAW chain
(pre-calibrator) -- the same quantity loss_attribution tracks as the
chronic +18pp bias.

This mirrors the precedent set by the Alt-A shadow-override replay:
no Stage-1 cache earns a promotion case on build stats alone.

Outputs:
  data/analysis_output/stage1_nb_replay/stage1_nb_replay_report.json
  data/analysis_output/stage1_nb_replay/stage1_nb_replay_report.md
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

PROJECT_DIR = Path(__file__).resolve().parents[2]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

DEFAULT_CANDIDATE_TABLE = (
    PROJECT_DIR / "data" / "analysis_output"
    / "calibration_opportunity_training"
    / "calibration_opportunity_training_table.jsonl"
)
DEFAULT_BET_TABLE = (
    PROJECT_DIR / "data" / "analysis_output" / "training_tables"
    / "signal_training_table.jsonl"
)
DEFAULT_PROD_CACHE = PROJECT_DIR / "cache" / "mlb_ou_cache.json"
DEFAULT_NB_CACHE = PROJECT_DIR / "cache" / "mlb_ou_cache_nb.staging.json"
DEFAULT_ALT_A_CACHE = PROJECT_DIR / "cache" / "mlb_ou_cache_alt_a.staging.json"
DEFAULT_OUTPUT_ROOT = (
    PROJECT_DIR / "data" / "analysis_output" / "stage1_nb_replay"
)

# The loss-attribution danger zone: raw-chain FV at or above this is
# where the audit measured the +24-28pp overconfidence.
HIGH_FV_THRESHOLD = 0.85
_EPS = 1e-6


def _clamp(p: float) -> float:
    return min(max(float(p), _EPS), 1.0 - _EPS)


def _logit(p: float) -> float:
    cp = _clamp(p)
    return math.log(cp / (1.0 - cp))


def _sigmoid(x: float) -> float:
    if x >= 0:
        z = math.exp(-x)
        return 1.0 / (1.0 + z)
    z = math.exp(x)
    return z / (1.0 + z)


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Out-of-sample Stage-1 cache replay (NB / Alt-A vs production).",
    )
    p.add_argument("--candidate-table", type=Path, default=DEFAULT_CANDIDATE_TABLE)
    p.add_argument("--bet-table", type=Path, default=DEFAULT_BET_TABLE)
    p.add_argument("--prod-cache", type=Path, default=DEFAULT_PROD_CACHE)
    p.add_argument("--nb-cache", type=Path, default=DEFAULT_NB_CACHE)
    p.add_argument(
        "--alt-a-cache", type=Path, default=DEFAULT_ALT_A_CACHE,
        help="Optional third variant; skipped cleanly when missing.",
    )
    p.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    p.add_argument(
        "--min-session-date", type=str, default="2026-01-01",
        help=(
            "Rows before this date are excluded. Default 2026-01-01: the "
            "staging caches train on <=2025, so 2026 rows are the "
            "out-of-sample universe."
        ),
    )
    return p.parse_args(argv)


def _load_cells(path: Path) -> Dict[str, dict]:
    with open(path, encoding="utf-8") as f:
        payload = json.load(f)
    cells = payload.get("cells")
    if not isinstance(cells, dict):
        raise ValueError(f"{path} has no cells block")
    return cells


def _load_jsonl(path: Path) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    with open(path, encoding="utf-8") as f:
        for raw in f:
            raw = raw.strip()
            if raw:
                rows.append(json.loads(raw))
    return rows


def replay_row(
    row: Dict[str, Any],
    *,
    prod_cells: Dict[str, dict],
    variant_cells: Dict[str, dict],
) -> Optional[Dict[str, float]]:
    """Delta-substitution replay of one settled row against a variant
    cache. Returns per-variant p0/p3 or None when the row can't be
    replayed (missing cell/line in either cache, boundary values).
    """
    cell_key = row.get("inferred_state_cell_key")
    line_key = row.get("inferred_state_line_key_poisson")
    base = row.get("base_fair_value")
    s2 = row.get("stage2_run_env_delta")
    s3 = row.get("team_offense_delta")
    if not cell_key or not line_key or base is None or s2 is None or s3 is None:
        return None
    prod_cell = prod_cells.get(str(cell_key))
    var_cell = variant_cells.get(str(cell_key))
    if not prod_cell or not var_cell:
        return None
    prod_po = prod_cell.get(str(line_key))
    var_po = var_cell.get(str(line_key))
    if prod_po is None or var_po is None:
        return None
    if not (0.0 < prod_po < 1.0) or not (0.0 < var_po < 1.0):
        return None

    delta = _logit(var_po) - _logit(prod_po)
    p0_prod = _clamp(float(base))
    p0_var = _sigmoid(_logit(p0_prod) + delta)
    chain = float(s2) + float(s3)
    p3_prod = _sigmoid(_logit(p0_prod) + chain)
    p3_var = _sigmoid(_logit(p0_var) + chain)
    return {
        "p0_prod": p0_prod,
        "p0_var": p0_var,
        "p3_prod": p3_prod,
        "p3_var": p3_var,
        "cache_po_prod": float(prod_po),
        "cache_po_var": float(var_po),
    }


def _new_agg() -> Dict[str, Any]:
    return {
        "n": 0,
        "wins": 0,
        "sum_p0_prod": 0.0, "sum_p0_var": 0.0,
        "sum_p3_prod": 0.0, "sum_p3_var": 0.0,
        "brier_prod": 0.0, "brier_var": 0.0,
        "logloss_prod": 0.0, "logloss_var": 0.0,
    }


def _accumulate(agg: Dict[str, Any], rp: Dict[str, float], won: int) -> None:
    agg["n"] += 1
    agg["wins"] += int(bool(won))
    agg["sum_p0_prod"] += rp["p0_prod"]
    agg["sum_p0_var"] += rp["p0_var"]
    agg["sum_p3_prod"] += rp["p3_prod"]
    agg["sum_p3_var"] += rp["p3_var"]
    y = float(bool(won))
    for tag in ("prod", "var"):
        p = _clamp(rp[f"p3_{tag}"])
        agg[f"brier_{tag}"] += (y - p) ** 2
        agg[f"logloss_{tag}"] += -(y * math.log(p) + (1 - y) * math.log(1 - p))


def _finalize(agg: Dict[str, Any]) -> Dict[str, Any]:
    n = agg["n"]
    if n == 0:
        return {"n": 0}
    won_rate = agg["wins"] / n
    out = {
        "n": n,
        "wins": agg["wins"],
        "realized_wr": round(won_rate, 4),
        "mean_p0_prod": round(agg["sum_p0_prod"] / n, 4),
        "mean_p0_variant": round(agg["sum_p0_var"] / n, 4),
        "mean_p3_prod": round(agg["sum_p3_prod"] / n, 4),
        "mean_p3_variant": round(agg["sum_p3_var"] / n, 4),
        "bias_p3_prod_pp": round((agg["sum_p3_prod"] / n - won_rate) * 100, 2),
        "bias_p3_variant_pp": round((agg["sum_p3_var"] / n - won_rate) * 100, 2),
        "brier_prod": round(agg["brier_prod"] / n, 4),
        "brier_variant": round(agg["brier_var"] / n, 4),
        "logloss_prod": round(agg["logloss_prod"] / n, 4),
        "logloss_variant": round(agg["logloss_var"] / n, 4),
    }
    out["bias_reduction_pp"] = round(
        abs(out["bias_p3_prod_pp"]) - abs(out["bias_p3_variant_pp"]), 2,
    )
    return out


def _inning_bucket(v: Any) -> str:
    try:
        i = int(v)
    except (TypeError, ValueError):
        return "unknown"
    if i <= 5:
        return "<=5"
    if i <= 7:
        return "6-7"
    return ">=8"


def _line_bucket(v: Any) -> str:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return "unknown"
    if f <= 6.5:
        return "<=6.5"
    if f <= 8.5:
        return "7.5-8.5"
    return ">=9.5"


def replay_table(
    rows: List[Dict[str, Any]],
    *,
    prod_cells: Dict[str, dict],
    variant_cells: Dict[str, dict],
    label_field: str,
    min_session_date: str,
) -> Dict[str, Any]:
    """Replay every eligible settled row; aggregate overall, by the
    high-FV danger zone, and by cohort dimensions."""
    overall = _new_agg()
    high_fv = _new_agg()
    cohorts: Dict[str, Dict[str, Dict[str, Any]]] = {
        "fallback_level": defaultdict(_new_agg),
        "inning_bucket": defaultdict(_new_agg),
        "line_bucket": defaultdict(_new_agg),
        "month": defaultdict(_new_agg),
        "mode": defaultdict(_new_agg),
    }
    skipped = defaultdict(int)
    cache_drift_abs = []  # |stored base_poisson - current prod cache po| on exact rows

    for row in rows:
        won = row.get(label_field)
        if won is None:
            skipped["no_label"] += 1
            continue
        if str(row.get("session_date") or "") < min_session_date:
            skipped["pre_window"] += 1
            continue
        if str(row.get("side") or "over").lower() != "over":
            skipped["non_over_side"] += 1
            continue
        rp = replay_row(row, prod_cells=prod_cells, variant_cells=variant_cells)
        if rp is None:
            skipped["unreplayable"] += 1
            continue

        won_i = int(bool(won))
        _accumulate(overall, rp, won_i)
        if rp["p3_prod"] >= HIGH_FV_THRESHOLD:
            _accumulate(high_fv, rp, won_i)
        fl = row.get("inferred_state_fallback_level")
        _accumulate(
            cohorts["fallback_level"][
                "exact" if fl == 0 else f"fallback_{fl}"
            ], rp, won_i,
        )
        _accumulate(cohorts["inning_bucket"][_inning_bucket(row.get("inning"))], rp, won_i)
        _accumulate(cohorts["line_bucket"][_line_bucket(row.get("line"))], rp, won_i)
        _accumulate(cohorts["month"][str(row.get("session_date"))[:7]], rp, won_i)
        _accumulate(cohorts["mode"][str(row.get("mode") or "unknown")], rp, won_i)

        # Cache-drift sanity: on exact-support rows the stored runtime
        # base_poisson should sit near the CURRENT production cache po
        # (daily rebuilds shift values slightly; large drift would mean
        # the delta-substitution anchors are stale).
        if fl == 0:
            stored = row.get("inferred_state_base_poisson")
            if stored is not None:
                cache_drift_abs.append(abs(float(stored) - rp["cache_po_prod"]))

    return {
        "label_field": label_field,
        "rows_in": len(rows),
        "skipped": dict(skipped),
        "overall": _finalize(overall),
        "high_fv_zone": {
            "threshold": HIGH_FV_THRESHOLD,
            **_finalize(high_fv),
        },
        "cohorts": {
            dim: {k: _finalize(v) for k, v in sorted(buckets.items())}
            for dim, buckets in cohorts.items()
        },
        "cache_drift_sanity": {
            "n_exact_rows": len(cache_drift_abs),
            "mean_abs_drift": (
                round(sum(cache_drift_abs) / len(cache_drift_abs), 4)
                if cache_drift_abs else None
            ),
            "max_abs_drift": (
                round(max(cache_drift_abs), 4) if cache_drift_abs else None
            ),
        },
    }


def _md_section(title: str, rep: Dict[str, Any], variant_name: str) -> List[str]:
    lines = [f"## {title}", ""]
    ov = rep["overall"]
    hf = rep["high_fv_zone"]
    if ov.get("n", 0) == 0:
        lines.append("_No replayable rows._")
        return lines
    lines.append(
        f"- Replayed **{ov['n']}** settled rows "
        f"(skipped: {rep['skipped']}); realized WR {ov['realized_wr']:.1%}."
    )
    lines.append(
        f"- Raw-chain bias: production **{ov['bias_p3_prod_pp']:+.2f}pp** "
        f"vs {variant_name} **{ov['bias_p3_variant_pp']:+.2f}pp** "
        f"(|bias| reduction {ov['bias_reduction_pp']:+.2f}pp)."
    )
    lines.append(
        f"- Brier {ov['brier_prod']:.4f} -> {ov['brier_variant']:.4f}; "
        f"logloss {ov['logloss_prod']:.4f} -> {ov['logloss_variant']:.4f}."
    )
    if hf.get("n", 0) > 0:
        lines.append(
            f"- High-FV zone (p3 >= {hf['threshold']}): n={hf['n']}, "
            f"bias {hf['bias_p3_prod_pp']:+.2f}pp -> "
            f"{hf['bias_p3_variant_pp']:+.2f}pp."
        )
    drift = rep["cache_drift_sanity"]
    lines.append(
        f"- Anchor sanity (exact rows, stored base vs current prod cache): "
        f"mean |drift| {drift['mean_abs_drift']}, max {drift['max_abs_drift']} "
        f"on n={drift['n_exact_rows']}."
    )
    lines.append("")
    lines.append("| Cohort | n | WR | prod bias | variant bias | Δ|bias| |")
    lines.append("| --- | ---: | ---: | ---: | ---: | ---: |")
    for dim, buckets in rep["cohorts"].items():
        for bucket, agg in buckets.items():
            if agg.get("n", 0) == 0:
                continue
            lines.append(
                f"| {dim}={bucket} | {agg['n']} | {agg['realized_wr']:.1%} "
                f"| {agg['bias_p3_prod_pp']:+.2f}pp "
                f"| {agg['bias_p3_variant_pp']:+.2f}pp "
                f"| {agg['bias_reduction_pp']:+.2f}pp |"
            )
    lines.append("")
    return lines


def build_payload(args: argparse.Namespace) -> Dict[str, Any]:
    prod_cells = _load_cells(args.prod_cache)
    nb_cells = _load_cells(args.nb_cache)
    candidate_rows = _load_jsonl(args.candidate_table)
    bet_rows = _load_jsonl(args.bet_table) if args.bet_table.exists() else []

    payload: Dict[str, Any] = {
        "schema_version": 1,
        "generated_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "method": "delta_substitution_logit_chain",
        "min_session_date": args.min_session_date,
        "caches": {
            "production": str(args.prod_cache),
            "nb": str(args.nb_cache),
            "alt_a": str(args.alt_a_cache) if args.alt_a_cache.exists() else None,
        },
        "variants": {},
    }

    payload["variants"]["nb_vs_prod"] = {
        "candidates": replay_table(
            candidate_rows,
            prod_cells=prod_cells, variant_cells=nb_cells,
            label_field="target_over_win",
            min_session_date=args.min_session_date,
        ),
        "bets": replay_table(
            bet_rows,
            prod_cells=prod_cells, variant_cells=nb_cells,
            label_field="target_win",
            min_session_date=args.min_session_date,
        ) if bet_rows else None,
    }

    if args.alt_a_cache.exists():
        alt_cells = _load_cells(args.alt_a_cache)
        payload["variants"]["alt_a_vs_prod"] = {
            "candidates": replay_table(
                candidate_rows,
                prod_cells=prod_cells, variant_cells=alt_cells,
                label_field="target_over_win",
                min_session_date=args.min_session_date,
            ),
            "bets": replay_table(
                bet_rows,
                prod_cells=prod_cells, variant_cells=alt_cells,
                label_field="target_win",
                min_session_date=args.min_session_date,
            ) if bet_rows else None,
        }

    return payload


def render_markdown(payload: Dict[str, Any]) -> str:
    lines = [
        "# Stage-1 out-of-sample cache replay",
        "",
        f"_Generated {payload['generated_at_utc']}; method "
        f"{payload['method']}; rows from {payload['min_session_date']} "
        "onward (out-of-sample vs the caches' <=2025 training window)._",
        "",
    ]
    for vname, tables in payload["variants"].items():
        pretty = "NB tail" if vname.startswith("nb") else "Alt-A (full cache)"
        lines.extend(_md_section(
            f"{pretty} — candidate universe", tables["candidates"], pretty,
        ))
        if tables.get("bets"):
            lines.extend(_md_section(
                f"{pretty} — placed bets", tables["bets"], pretty,
            ))
    lines.append(
        "_Read this with: bias is mean(raw-chain p3) - realized WR; the "
        "calibrator currently absorbs production's gap downstream, so a "
        "variant that closes raw bias shrinks the work (and the failure "
        "modes) of every layer above Stage-1. Promotion still goes "
        "through promote.py stage1 + the O/P fleet arms._"
    )
    return "\n".join(lines) + "\n"


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    payload = build_payload(args)

    # Best-effort lineage, matching other builders.
    try:
        from scripts.analysis.artifact_lineage import compute_lineage
        payload["lineage"] = compute_lineage(
            builder_path=__file__,
            input_paths=[
                args.candidate_table, args.bet_table,
                args.prod_cache, args.nb_cache,
            ],
            project_root=PROJECT_DIR,
        )
    except Exception:  # noqa: BLE001
        pass

    args.output_root.mkdir(parents=True, exist_ok=True)
    json_path = args.output_root / "stage1_nb_replay_report.json"
    md_path = args.output_root / "stage1_nb_replay_report.md"
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    md_path.write_text(render_markdown(payload), encoding="utf-8")
    print(f"Wrote {json_path}")
    print(f"Wrote {md_path}")

    nb = payload["variants"]["nb_vs_prod"]["candidates"]["overall"]
    if nb.get("n"):
        print(
            f"NB vs prod (candidates, n={nb['n']}): bias "
            f"{nb['bias_p3_prod_pp']:+.2f}pp -> {nb['bias_p3_variant_pp']:+.2f}pp, "
            f"brier {nb['brier_prod']:.4f} -> {nb['brier_variant']:.4f}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
