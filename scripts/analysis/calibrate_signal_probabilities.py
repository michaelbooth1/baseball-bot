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
        "--min-train-date",
        type=str,
        default=None,
        help=(
            "Hygiene #24 (2026-05-20): restrict calibrator training to "
            "samples with session_date >= this date (YYYY-MM-DD). Used "
            "to exclude pre-model-upgrade rows when an upgrade has "
            "shifted the input distribution (e.g., set to '2026-05-08' "
            "after TR21 to drop pre-density_alt+hr_factor rows). "
            "Default None = include all rows (back-compat)."
        ),
    )
    p.add_argument(
        "--per-line-min-rows",
        type=int,
        default=0,
        help=(
            "Per-line stratified calibration (2026-06-06): when >0, fit "
            "an additional Platt/isotonic curve per (family, line) whose "
            "labeled sample count >= this threshold. Stored under "
            "families[<family>][lines][<line>] in the calibration "
            "artifact; pooled family curve remains the fallback for "
            "lines below threshold or not present. Default 0 = disabled "
            "(back-compat). Recommended starting value 100; cohort "
            "analysis shows line 5.5 needs its own curve (realized WR "
            "55% at raw FV>=0.90 vs ~80% for other lines)."
        ),
    )
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



# Refactored 2026-05-25: methods, stability-gate, input-drift moved
# to scripts/analysis/calibration/. Re-exported here so existing
# imports ("import calibrate_signal_probabilities as cal" then
# cal._fit_platt etc.) keep working.
from scripts.analysis.calibration.methods import (  # noqa: F401
    _fit_platt, _predict_platt, _fit_isotonic, _predict_isotonic,
    _metrics_bundle, _select_best_method,
)
from scripts.analysis.calibration.stability_gate import (  # noqa: F401
    _load_selection_history, _history_row_date, _trailing_family_history,
    _modal_selection, _apply_stability_gate, _write_selection_history_row,
)
from scripts.analysis.calibration.input_drift import (  # noqa: F401
    _load_input_drift_status,
)
from scripts.analysis.calibration.scoring import (  # noqa: F401
    _clip_prob, _stable_sigmoid, _logit, _logloss, _brier,
    _ece, _reliability_bins, _slice_overconfidence,
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
    """Orchestrator. Splits dates, fits Platt+isotonic on train (and
    on all rows when artifact_purpose='runtime-refit'), then calls the
    4 phase helpers in scripts/analysis/calibration/bundle_phases.py
    to score / select / build payloads / build pred rows.

    Refactored 2026-05-25 (Tier 2.5): function used to be 311 lines.
    Behavior is identical to the pre-refactor version; the phases are
    pure decomposition.
    """
    from scripts.analysis.calibration.bundle_phases import (
        _score_methods_on_splits,
        _select_method_with_audits,
        _build_calibration_payload,
        _build_prediction_rows,
    )

    # Phase 1: splits + train-fit (inline; tightly coupled to the
    # function args + strict-mode checks).
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

    # Phase 2 + part of Phase 3: predictions + method_eval.
    method_predictions, export_method_predictions, method_eval = _score_methods_on_splits(
        train_probs=train_probs, train_labels=train_labels,
        val_probs=val_probs, val_labels=val_labels,
        test_probs=test_probs, test_labels=test_labels,
        platt_params=platt_params, isotonic_params=isotonic_params,
        export_platt_params=export_platt_params,
        export_isotonic_params=export_isotonic_params,
        artifact_purpose=artifact_purpose,
    )

    # Phase 3: selection + audits (identity-rejection, stability,
    # input-drift; emits WARNING logs as side effects).
    selected_method, selection_audit = _select_method_with_audits(
        method_eval,
        model_family=model_family,
        max_date=max_date,
        identity_rejection_train_ece_delta=identity_rejection_train_ece_delta,
        stability_history=stability_history,
        stability_window=stability_window,
        stability_min_history=stability_min_history,
        stability_gate_enabled=stability_gate_enabled,
        input_drift_status=input_drift_status,
    )

    # Phase 4: payloads.
    family_counts: Dict[str, int] = {}
    for sample in samples:
        family_counts[sample.model_family] = family_counts.get(sample.model_family, 0) + 1

    calibration_payload, report_payload = _build_calibration_payload(
        now_iso=_now_iso(),
        selected_method=selected_method,
        selection_audit=selection_audit,
        method_eval=method_eval,
        platt_params=platt_params,
        isotonic_params=isotonic_params,
        export_platt_params=export_platt_params,
        export_isotonic_params=export_isotonic_params,
        side=side,
        input_path=str(input_path),
        input_kind=input_kind,
        mode=mode,
        family_mode=family_mode,
        model_family=model_family,
        min_date=min_date,
        max_date=max_date,
        n_total=len(samples),
        n_train=len(train),
        n_val=len(val),
        n_test=len(test),
        split_dates=split_dates,
        skipped_reasons=skipped_reasons,
        probability_source_counts=probability_source_counts,
        family_counts=family_counts,
        artifact_purpose=artifact_purpose,
        runtime_fit_scope=runtime_fit_scope,
    )

    # Phase 5: per-bet prediction rows for the report sidecar.
    pred_rows = _build_prediction_rows(
        split_buckets={"train": train, "validation": val, "test": test},
        method_predictions=method_predictions,
        export_method_predictions=export_method_predictions,
        selected_method=selected_method,
        artifact_purpose=artifact_purpose,
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

    # Hygiene #24: --min-train-date filter. Drops samples with
    # session_date < threshold so the operator can fit a
    # post-upgrade-only calibrator without blending pre/post-regime
    # rows. Date comparison is lexicographic since session_date is
    # always ISO-8601 YYYY-MM-DD. Logged so the operator can verify
    # the filter took effect.
    min_train_date_filter_count = 0
    if getattr(args, "min_train_date", None):
        cutoff = str(args.min_train_date)
        # Validate format early -- a bad cutoff would silently drop
        # everything since "" < every ISO date.
        if not (
            len(cutoff) == 10 and cutoff[4] == "-" and cutoff[7] == "-"
        ):
            raise SystemExit(
                f"--min-train-date must be YYYY-MM-DD, got {cutoff!r}"
            )
        kept = []
        for s in samples:
            sd = s.session_date or ""
            if sd >= cutoff:
                kept.append(s)
            else:
                min_train_date_filter_count += 1
        LOGGER.info(
            "min_train_date filter: cutoff=%s, kept=%d, dropped=%d "
            "(from %d total before filter)",
            cutoff,
            len(kept),
            min_train_date_filter_count,
            len(samples),
        )
        samples = kept

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
        per_line_min_rows = max(0, int(getattr(args, "per_line_min_rows", 0) or 0))
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

            # Per-line stratification (2026-06-06). Runtime reads
            # families[<family>][lines][<line>] first; falls back to the
            # pooled family curve when (family, line) is absent. We fit
            # ONLY for line cohorts with >= per_line_min_rows labeled
            # samples; rare lines (4.5, 11.5+) stay on the pooled curve.
            # Disabled when threshold==0 to preserve back-compat.
            if per_line_min_rows > 0:
                by_line: Dict[str, List[Sample]] = {}
                for s in by_family[family]:
                    ln = s.line if s.line is not None else "unknown"
                    by_line.setdefault(ln, []).append(s)
                line_payloads: Dict[str, Dict[str, Any]] = {}
                line_reports: Dict[str, Dict[str, Any]] = {}
                for line_key in sorted(by_line.keys()):
                    line_samples = by_line[line_key]
                    if len(line_samples) < per_line_min_rows:
                        continue
                    line_payload, line_report, line_preds = _fit_calibration_bundle(
                        list(line_samples),
                        input_path=args.input_path,
                        mode=args.mode,
                        min_date=args.min_date,
                        max_date=args.max_date,
                        val_frac=args.val_frac,
                        test_frac=args.test_frac,
                        input_kind=args.input_kind,
                        family_mode=args.family_mode,
                        model_family=family,
                        strict=False,  # never strict on per-line; pooled is the safety net
                        skipped_reasons={},
                        probability_source_counts=probability_source_counts,
                        artifact_purpose=args.artifact_purpose,
                        identity_rejection_train_ece_delta=args.identity_rejection_train_ece_delta,
                        stability_history=[],  # no stability gate per line; sample too small
                        stability_window=args.stability_window,
                        stability_min_history=args.stability_min_history,
                        stability_gate_enabled=False,
                        input_drift_status=input_drift_status,
                        side=args.side,
                    )
                    # Annotate predictions so per-line attribution is
                    # downstream-attributable in the predictions JSONL.
                    for pr in line_preds:
                        pr["stratification_scope"] = "per_line"
                        pr["stratification_line"] = line_key
                    pred_rows.extend(line_preds)
                    line_payloads[line_key] = {
                        "selected_method": line_payload["selected_method"],
                        "selection_metric": line_payload.get(
                            "selection_metric", "validation_logloss",
                        ),
                        "selection_audit": line_payload.get("selection_audit", {}),
                        "methods": line_payload["methods"],
                        "n_train": len(line_samples),
                    }
                    line_reports[line_key] = line_report
                    LOGGER.info(
                        "per_line fit: family=%s line=%s n=%d method=%s",
                        family, line_key, len(line_samples),
                        line_payload["selected_method"],
                    )
                if line_payloads:
                    family_payload["lines"] = line_payloads
                    family_report["line_reports"] = line_reports

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
                    "min_train_date": getattr(args, "min_train_date", None),
                    "min_train_date_filter_count": (
                        min_train_date_filter_count
                    ),
                    "per_line_min_rows": int(
                        getattr(args, "per_line_min_rows", 0) or 0
                    ),
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
