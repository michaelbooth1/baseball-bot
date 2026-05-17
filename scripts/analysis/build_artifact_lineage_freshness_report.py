#!/usr/bin/env python3
"""
Build a canonical artifact lineage/freshness report.

This is a diagnostic-only guard for the analysis stack. It checks the durable
runtime inputs, shared caches, canonical analysis tables, model artifacts, and
operator reports that drive the MLB Polymarket research loop.

For each artifact it records:
  - generated_at_utc / max_date when exposed by the artifact
  - primary path, mtime, and row/family counts
  - explicit upstream input paths with mtime and a content/listing hash
  - whether the artifact is older than any upstream input
  - whether the artifact max_date lags an upstream artifact max_date

Outputs:
  data/analysis_output/artifact_lineage_freshness/
    artifact_lineage_freshness_report.json
    artifact_lineage_freshness_report.md
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple


PROJECT_DIR = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT_ROOT = PROJECT_DIR / "data" / "analysis_output" / "artifact_lineage_freshness"
DEFAULT_OUTPUT_STEM = "artifact_lineage_freshness_report"

DATE_KEYS = (
    "session_date",
    "date",
    "as_of_date",
    "data_max_date",
    "max_date",
    "game_date",
    "completed_date",
)
FAMILY_KEYS = ("signal_model_family", "model_family", "family")
DEFAULT_MAX_HASH_BYTES = 64 * 1024 * 1024
DEFAULT_MAX_JSONL_SCAN_BYTES = 128 * 1024 * 1024


@dataclass(frozen=True)
class ArtifactSpec:
    name: str
    category: str
    primary_path: Path
    input_paths: Tuple[Path, ...] = ()
    manifest_path: Optional[Path] = None
    row_path: Optional[Path] = None
    optional: bool = False
    notes: str = ""
    allow_large_row_scan: bool = False


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _iso_from_timestamp(ts: Optional[float]) -> Optional[str]:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _path_str(path: Path) -> str:
    try:
        return str(path.resolve())
    except OSError:
        return str(path)


def _norm_path(path: Path) -> str:
    try:
        return str(path.resolve()).lower()
    except OSError:
        return str(path).lower()


def _read_json(path: Path) -> Dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _safe_int(value: Any) -> Optional[int]:
    try:
        if value is None or value == "":
            return None
        if isinstance(value, bool):
            return int(value)
        out = int(float(value))
        if out < 0:
            return None
        return out
    except (TypeError, ValueError, OverflowError):
        return None


def _is_date_string(value: Any) -> bool:
    if not isinstance(value, str) or len(value) < 10:
        return False
    text = value[:10]
    try:
        datetime.strptime(text, "%Y-%m-%d")
        return True
    except ValueError:
        return False


def _coerce_date(value: Any) -> Optional[str]:
    if _is_date_string(value):
        return str(value)[:10]
    return None


def _coerce_generated_at(data: Mapping[str, Any]) -> Optional[str]:
    for key in ("generated_at_utc", "generated_at", "created_at_utc", "built_at_utc", "built_at", "built"):
        value = data.get(key)
        if isinstance(value, str) and value:
            return value
    meta = data.get("meta")
    if isinstance(meta, Mapping):
        for key in ("generated_at_utc", "generated_at", "built_at_utc", "built_at", "built"):
            value = meta.get(key)
            if isinstance(value, str) and value:
                return value
    return None


def _first_path(data: Mapping[str, Any], paths: Sequence[Sequence[str]]) -> Any:
    for path in paths:
        cur: Any = data
        ok = True
        for key in path:
            if not isinstance(cur, Mapping) or key not in cur:
                ok = False
                break
            cur = cur[key]
        if ok and cur not in (None, ""):
            return cur
    return None


def _extract_max_date(data: Mapping[str, Any]) -> Optional[str]:
    direct_paths = (
        ("max_date",),
        ("data_max_date",),
        ("as_of_date",),
        ("session_date",),
        ("config", "max_date"),
        ("config", "data_max_date"),
        ("config", "as_of_date"),
        ("source", "max_date"),
        ("source", "data_max_date"),
        ("source_summary", "max_date"),
        ("source_summary", "data_max_date"),
        ("meta", "history_end_date"),
        ("meta", "max_date"),
        ("meta", "data_max_date"),
    )
    for path in direct_paths:
        date_value = _coerce_date(_first_path(data, (path,)))
        if date_value:
            return date_value

    session_counts = data.get("session_date_counts")
    if isinstance(session_counts, Mapping):
        dates = [_coerce_date(k) for k in session_counts.keys()]
        dates = [d for d in dates if d]
        if dates:
            return max(dates)

    split_dates = _first_path(data, (("split_dates",), ("counts", "dates_by_split"), ("dates_by_split",)))
    if isinstance(split_dates, Mapping):
        dates: List[str] = []
        for values in split_dates.values():
            if isinstance(values, Sequence) and not isinstance(values, (str, bytes)):
                dates.extend(d for d in (_coerce_date(v) for v in values) if d)
        if dates:
            return max(dates)

    return None


def _extract_row_count(data: Mapping[str, Any]) -> Optional[int]:
    paths = (
        ("row_count",),
        ("rows_total",),
        ("total_rows",),
        ("source_rows",),
        ("n_rows",),
        ("counts", "training_rows_total"),
        ("counts", "filtered_rows_total"),
        ("counts", "source_rows_total"),
        ("counts", "rows_total"),
        ("row_counts", "analysis_safe_rows"),
        ("row_counts", "total_rows"),
        ("summary", "rows"),
        ("readiness", "n_filled"),
    )
    for path in paths:
        out = _safe_int(_first_path(data, (path,)))
        if out is not None:
            return out
    rows = data.get("rows")
    if isinstance(rows, int):
        return rows
    if isinstance(rows, Mapping):
        out = _safe_int(rows.get("total") or rows.get("overall") or rows.get("all"))
        if out is not None:
            return out
    return None


def _family_count_from_value(value: Any) -> Optional[int]:
    if isinstance(value, int):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return int(value)
    if isinstance(value, Mapping):
        for key in ("total", "rows", "n", "count", "final_labels"):
            out = _safe_int(value.get(key))
            if out is not None:
                return out
        rows = value.get("rows")
        if isinstance(rows, Mapping):
            out = _safe_int(rows.get("total") or rows.get("overall") or rows.get("all"))
            if out is not None:
                return out
    return None


def _extract_family_counts(data: Mapping[str, Any]) -> Dict[str, int]:
    for path in (
        ("counts", "rows_by_family"),
        ("rows_by_family",),
        ("family_counts",),
        ("families",),
        ("by_family",),
    ):
        value = _first_path(data, (path,))
        if not isinstance(value, Mapping):
            continue
        out: Dict[str, int] = {}
        for family, count_value in value.items():
            count = _family_count_from_value(count_value)
            if count is not None:
                out[str(family)] = count
        if out:
            return out
    return {}


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _inspect_file(path: Path, max_hash_bytes: int) -> Dict[str, Any]:
    st = path.stat()
    size = int(st.st_size)
    info: Dict[str, Any] = {
        "path": _path_str(path),
        "exists": True,
        "kind": "file",
        "size_bytes": size,
        "mtime_utc": _iso_from_timestamp(st.st_mtime),
        "content_sha256": None,
        "hash_status": "not_requested",
    }
    if size <= max_hash_bytes:
        try:
            info["content_sha256"] = _sha256_file(path)
            info["hash_status"] = "ok"
        except OSError as exc:
            info["hash_status"] = f"error:{exc.__class__.__name__}"
    else:
        info["hash_status"] = f"skipped_file_larger_than_{max_hash_bytes}_bytes"
    return info


def _inspect_directory(path: Path, max_entries: int = 100_000) -> Dict[str, Any]:
    count = 0
    total_bytes = 0
    newest_mtime: Optional[float] = None
    digest = hashlib.sha256()
    truncated = False
    try:
        files = sorted((p for p in path.rglob("*") if p.is_file()), key=lambda p: str(p).lower())
        if len(files) > max_entries:
            files = files[:max_entries]
            truncated = True
        for child in files:
            try:
                st = child.stat()
            except OSError:
                continue
            count += 1
            total_bytes += int(st.st_size)
            newest_mtime = st.st_mtime if newest_mtime is None else max(newest_mtime, st.st_mtime)
            try:
                rel = child.relative_to(path).as_posix()
            except ValueError:
                rel = child.as_posix()
            digest.update(rel.encode("utf-8", errors="replace"))
            digest.update(b"|")
            digest.update(str(int(st.st_size)).encode("ascii"))
            digest.update(b"|")
            digest.update(str(int(st.st_mtime_ns)).encode("ascii"))
            digest.update(b"\n")
    except OSError:
        truncated = True

    try:
        dir_mtime = path.stat().st_mtime
    except OSError:
        dir_mtime = None
    mtime = newest_mtime if newest_mtime is not None else dir_mtime
    return {
        "path": _path_str(path),
        "exists": True,
        "kind": "directory",
        "file_count": count,
        "size_bytes": total_bytes,
        "mtime_utc": _iso_from_timestamp(mtime),
        "directory_listing_sha256": digest.hexdigest(),
        "hash_status": "truncated_listing" if truncated else "ok",
    }


def inspect_path(path: Path, *, max_hash_bytes: int = DEFAULT_MAX_HASH_BYTES) -> Dict[str, Any]:
    if not path.exists():
        return {
            "path": _path_str(path),
            "exists": False,
            "kind": None,
            "size_bytes": None,
            "mtime_utc": None,
            "content_sha256": None,
            "directory_listing_sha256": None,
            "hash_status": "missing",
        }
    if path.is_dir():
        return _inspect_directory(path)
    return _inspect_file(path, max_hash_bytes=max_hash_bytes)


def _mtime_timestamp(path: Path) -> Optional[float]:
    try:
        if not path.exists():
            return None
        if path.is_dir():
            newest: Optional[float] = None
            for child in path.rglob("*"):
                if not child.is_file():
                    continue
                try:
                    mt = child.stat().st_mtime
                except OSError:
                    continue
                newest = mt if newest is None else max(newest, mt)
            if newest is not None:
                return newest
        return path.stat().st_mtime
    except OSError:
        return None


def _jsonl_stats(path: Path, max_scan_bytes: int) -> Dict[str, Any]:
    if not path.exists() or not path.is_file():
        return {"scanned": False, "reason": "missing"}
    try:
        size = path.stat().st_size
    except OSError:
        return {"scanned": False, "reason": "stat_error"}
    if size > max_scan_bytes:
        return {"scanned": False, "reason": f"larger_than_{max_scan_bytes}_bytes", "size_bytes": size}

    row_count = 0
    family_counts: Dict[str, int] = {}
    dates: List[str] = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                row_count += 1
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(row, Mapping):
                    for key in FAMILY_KEYS:
                        fam = row.get(key)
                        if fam not in (None, ""):
                            family_counts[str(fam)] = family_counts.get(str(fam), 0) + 1
                            break
                    for key in DATE_KEYS:
                        d = _coerce_date(row.get(key))
                        if d:
                            dates.append(d)
                            break
    except OSError as exc:
        return {"scanned": False, "reason": f"io_error:{exc.__class__.__name__}", "size_bytes": size}

    return {
        "scanned": True,
        "row_count": row_count,
        "family_counts": family_counts,
        "min_date": min(dates) if dates else None,
        "max_date": max(dates) if dates else None,
        "size_bytes": size,
    }


def _latest_matching_path(pattern: str, project_dir: Path) -> Path:
    paths = sorted(project_dir.glob(pattern))
    return paths[-1] if paths else project_dir / pattern.replace("*", "MISSING")


def canonical_artifact_specs(project_dir: Path = PROJECT_DIR) -> List[ArtifactSpec]:
    live_root = project_dir / "data" / "live_trading"
    analysis = project_dir / "data" / "analysis_output"
    cache = project_dir / "cache"
    games = project_dir / "data" / "games" / "regular"
    latest_weather = _latest_matching_path("cache/weather/game_weather_*.json", project_dir)
    latest_human_review = _latest_matching_path("data/analysis_output/daily_human_review/*_human_review.json", project_dir)
    latest_weekly_rollup = _latest_matching_path("data/analysis_output/weekly_rollup/*_weekly_rollup.html", project_dir)
    latest_stage1_audit = _latest_matching_path(
        "data/analysis_output/stage1_inferred_empirical_audit/*_stage1_inferred_empirical_audit.json",
        project_dir,
    )

    return [
        ArtifactSpec("raw_games_regular", "source", games, optional=True),
        ArtifactSpec("live_sessions", "source", live_root / "sessions", optional=True),
        ArtifactSpec("live_candidate_universe_raw", "source", live_root / "candidate_universe", optional=True),
        ArtifactSpec("live_orders_ledger", "source", live_root / "live_orders_ledger.jsonl", optional=True),
        ArtifactSpec("master_ledger", "source", live_root / "master_ledger.jsonl", optional=True),
        ArtifactSpec("stage1_ou_cache", "cache", cache / "mlb_ou_cache.json", input_paths=(games,)),
        ArtifactSpec(
            "stage2_run_env_cache",
            "cache",
            cache / "mlb_stage2_run_env.json",
            input_paths=(cache / "mlb_ou_cache.json", cache / "park_hr_factors.json", games),
            optional=True,
        ),
        ArtifactSpec(
            "stage2_run_env_staging_cache",
            "cache",
            cache / "mlb_stage2_run_env.staging.json",
            input_paths=(cache / "mlb_ou_cache.json", cache / "park_hr_factors.json", games),
            optional=True,
        ),
        ArtifactSpec("team_game_log_cache", "cache", cache / "team_game_log.json", input_paths=(games,), optional=True),
        ArtifactSpec("park_hr_factors_cache", "cache", cache / "park_hr_factors.json", input_paths=(games,), optional=True),
        ArtifactSpec("pitcher_cache", "cache", cache / "pitcher_cache.json", optional=True),
        ArtifactSpec("latest_game_weather_cache", "cache", latest_weather, optional=True),
        ArtifactSpec(
            "analysis_safe_trade_table",
            "table",
            analysis / "analysis_safe_trades" / "analysis_safe_trades_summary.json",
            row_path=analysis / "analysis_safe_trades" / "analysis_safe_trades.jsonl",
            input_paths=(live_root / "sessions", live_root / "live_orders_ledger.jsonl", live_root / "master_ledger.jsonl"),
        ),
        ArtifactSpec(
            "candidate_universe_table",
            "table",
            analysis / "candidate_universe" / "build_manifest.json",
            row_path=analysis / "candidate_universe" / "candidates_master.jsonl",
            input_paths=(live_root / "candidate_universe", games),
        ),
        ArtifactSpec(
            "calibration_opportunity_training_table",
            "table",
            analysis / "calibration_opportunity_training" / "calibration_opportunity_training_table_manifest.json",
            row_path=analysis / "calibration_opportunity_training" / "calibration_opportunity_training_table.jsonl",
            input_paths=(live_root / "candidate_universe", games),
        ),
        ArtifactSpec(
            "unified_signals_table",
            "table",
            analysis / "unified_signals" / "build_manifest.json",
            row_path=analysis / "unified_signals" / "signals_master.jsonl",
            input_paths=(
                live_root / "sessions",
                live_root / "master_ledger.jsonl",
                live_root / "live_orders_ledger.jsonl",
                live_root / "candidate_universe",
                cache / "mlb_ou_cache.json",
            ),
        ),
        ArtifactSpec(
            "signal_training_table",
            "table",
            analysis / "training_tables" / "signal_training_table_manifest.json",
            row_path=analysis / "training_tables" / "signal_training_table.jsonl",
            input_paths=(analysis / "unified_signals" / "signals_master.jsonl",),
        ),
        ArtifactSpec(
            "signal_win_calibration",
            "model",
            analysis / "calibration" / "signal_win_calibration.json",
            row_path=analysis / "calibration" / "signal_win_calibration_predictions.jsonl",
            input_paths=(analysis / "calibration_opportunity_training" / "calibration_opportunity_training_table.jsonl",),
        ),
        ArtifactSpec(
            "model_baseline_signal_win",
            "model",
            analysis / "model_baselines" / "signal_win_model.json",
            row_path=analysis / "model_baselines" / "signal_win_predictions.jsonl",
            input_paths=(analysis / "training_tables" / "signal_training_table.jsonl",),
        ),
        ArtifactSpec(
            "model_baseline_execution_fill",
            "model",
            analysis / "model_baselines" / "execution_fill_model.json",
            row_path=analysis / "model_baselines" / "execution_fill_predictions.jsonl",
            input_paths=(analysis / "training_tables" / "signal_training_table.jsonl",),
        ),
        ArtifactSpec(
            "ev_policy_report",
            "model",
            analysis / "ev_policy" / "ev_policy_report.json",
            row_path=analysis / "ev_policy" / "ev_scored_rows.jsonl",
            input_paths=(
                analysis / "training_tables" / "signal_training_table.jsonl",
                analysis / "model_baselines" / "signal_win_model.json",
                analysis / "model_baselines" / "execution_fill_model.json",
            ),
        ),
        ArtifactSpec(
            "ev_signal_win_if_filled_model",
            "model",
            analysis / "ev_policy" / "ev_signal_win_if_filled_model.json",
            input_paths=(analysis / "training_tables" / "signal_training_table.jsonl",),
        ),
        ArtifactSpec(
            "ev_execution_fill_runtime_model",
            "model",
            analysis / "ev_policy" / "ev_execution_fill_runtime_model.json",
            input_paths=(analysis / "training_tables" / "signal_training_table.jsonl",),
        ),
        ArtifactSpec(
            "ev_execution_fill_strict_model",
            "model",
            analysis / "ev_policy" / "ev_execution_fill_strict_model.json",
            input_paths=(analysis / "training_tables" / "signal_training_table.jsonl",),
        ),
        ArtifactSpec(
            "model_maturity_report",
            "report",
            analysis / "model_maturity" / "model_maturity_report.json",
            input_paths=(analysis / "calibration_opportunity_training" / "calibration_opportunity_training_table.jsonl",),
        ),
        ArtifactSpec(
            "fair_value_stage_ablation_report",
            "report",
            analysis / "fair_value_stage_ablation" / "fair_value_stage_ablation_report.json",
            input_paths=(analysis / "calibration_opportunity_training" / "calibration_opportunity_training_table.jsonl",),
        ),
        ArtifactSpec(
            "fv_gap_decomposition_report",
            "report",
            analysis / "fv_gap_decomposition" / "fv_gap_decomposition_report.json",
            input_paths=(analysis / "calibration_opportunity_training" / "calibration_opportunity_training_table.jsonl",),
        ),
        ArtifactSpec(
            "fv_trust_shrinkage_report",
            "report",
            analysis / "fv_trust_shrinkage" / "fv_trust_shrinkage_report.json",
            row_path=analysis / "fv_trust_shrinkage" / "fv_trust_shrinkage_predictions.jsonl",
            input_paths=(analysis / "calibration_opportunity_training" / "calibration_opportunity_training_table.jsonl",),
        ),
        ArtifactSpec(
            "calibration_market_anchored_alpha_report",
            "model",
            analysis / "calibration_market_anchored_alpha" / "calibration_market_anchored_alpha_report.json",
            row_path=analysis / "calibration_market_anchored_alpha" / "calibration_market_anchored_alpha_predictions.jsonl",
            input_paths=(analysis / "calibration_opportunity_training" / "calibration_opportunity_training_table.jsonl",),
        ),
        ArtifactSpec(
            "clv_report",
            "report",
            analysis / "clv" / "clv_summary.json",
            row_path=analysis / "clv" / "clv_rows.jsonl",
            input_paths=(
                analysis / "unified_signals" / "signals_master.jsonl",
                analysis / "unified_signals" / "signal_book_snapshots.jsonl",
                analysis / "analysis_safe_trades" / "analysis_safe_trades.jsonl",
                analysis / "calibration_opportunity_training" / "calibration_opportunity_training_table.jsonl",
            ),
            optional=True,
        ),
        ArtifactSpec(
            "fv_disagreement_quality_report",
            "report",
            analysis / "fv_disagreement_quality" / "fv_disagreement_quality_summary.json",
            row_path=analysis / "fv_disagreement_quality" / "fv_disagreement_quality_rows.jsonl",
            input_paths=(
                analysis / "calibration_opportunity_training" / "calibration_opportunity_training_table.jsonl",
                analysis / "clv" / "clv_rows.jsonl",
            ),
            optional=True,
        ),
        ArtifactSpec(
            "stage1_inferred_empirical_audit",
            "report",
            latest_stage1_audit,
            input_paths=(analysis / "calibration_opportunity_training" / "calibration_opportunity_training_table.jsonl",),
            optional=True,
        ),
        ArtifactSpec(
            "execution_diagnostics",
            "report",
            analysis / "execution_diagnostics" / "execution_diagnostics_summary.json",
            row_path=analysis / "execution_diagnostics" / "execution_diagnostics_master.jsonl",
            input_paths=(
                analysis / "unified_signals" / "signals_master.jsonl",
                analysis / "unified_signals" / "signal_book_snapshots.jsonl",
            ),
        ),
        ArtifactSpec(
            "queue_aware_execution_replay",
            "report",
            analysis / "execution_replay" / "queue_aware_execution_replay_summary.json",
            row_path=analysis / "execution_replay" / "queue_aware_execution_replay_rows.jsonl",
            input_paths=(
                analysis / "unified_signals" / "signals_master.jsonl",
                analysis / "unified_signals" / "signal_book_snapshots.jsonl",
            ),
        ),
        ArtifactSpec(
            "learned_execution_policy",
            "model",
            analysis / "execution_policy_prototype" / "learned_execution_policy_report.json",
            input_paths=(analysis / "execution_replay" / "queue_aware_execution_replay_rows.jsonl",),
            optional=True,
        ),
        ArtifactSpec(
            "state_value_transition_report",
            "report",
            analysis / "state_value_transition" / "state_value_transition_report.json",
            input_paths=(analysis / "candidate_universe" / "candidates_master.jsonl",),
        ),
        ArtifactSpec(
            "no_score_drift_policy",
            "report",
            analysis / "no_score_drift_policy" / "no_score_drift_policy_summary.json",
            row_path=analysis / "no_score_drift_policy" / "no_score_drift_policy_rows.jsonl",
            input_paths=(analysis / "candidate_universe" / "candidates_master.jsonl",),
        ),
        ArtifactSpec(
            "no_score_drift_paper_ledger",
            "report",
            analysis / "no_score_drift_paper_ledger" / "no_score_drift_paper_ledger_summary.json",
            row_path=analysis / "no_score_drift_paper_ledger" / "no_score_drift_paper_ledger_rows.jsonl",
            input_paths=(analysis / "calibration_opportunity_training" / "calibration_opportunity_training_table.jsonl",),
        ),
        ArtifactSpec(
            "no_score_drift_walk_forward",
            "report",
            analysis / "no_score_drift_walk_forward" / "summary.json",
            row_path=analysis / "no_score_drift_walk_forward" / "no_score_drift_training_rows.jsonl",
            input_paths=(analysis / "calibration_opportunity_training" / "calibration_opportunity_training_table.jsonl",),
            optional=True,
        ),
        ArtifactSpec(
            "calibration_market_anchored_alpha_walk_forward",
            "report",
            analysis / "calibration_market_anchored_alpha_walk_forward" / "summary.json",
            row_path=analysis / "calibration_market_anchored_alpha_walk_forward" / "predictions.jsonl",
            input_paths=(analysis / "calibration_opportunity_training" / "calibration_opportunity_training_table.jsonl",),
            optional=True,
        ),
        ArtifactSpec(
            "fv_disagreement_quality_walk_forward",
            "report",
            analysis / "fv_disagreement_quality_walk_forward" / "summary.json",
            row_path=analysis / "fv_disagreement_quality_walk_forward" / "predictions.jsonl",
            input_paths=(
                analysis / "calibration_opportunity_training" / "calibration_opportunity_training_table.jsonl",
                analysis / "clv" / "clv_rows.jsonl",
            ),
            optional=True,
        ),
        ArtifactSpec(
            "walk_forward_score_event",
            "report",
            analysis / "walk_forward" / "summary.json",
            row_path=analysis / "walk_forward" / "per_window_results.jsonl",
            input_paths=(analysis / "training_tables" / "signal_training_table.jsonl",),
            optional=True,
        ),
        ArtifactSpec(
            "walk_forward_certification",
            "report",
            analysis / "walk_forward_certification" / "walk_forward_certification.json",
            input_paths=(analysis / "training_tables" / "signal_training_table.jsonl",),
        ),
        ArtifactSpec(
            "analysis_safe_daily_human_review_latest",
            "report",
            latest_human_review,
            input_paths=(analysis / "analysis_safe_trades" / "analysis_safe_trades.jsonl",),
            optional=True,
        ),
        ArtifactSpec(
            "stake_scaling_analysis",
            "report",
            analysis / "stake_scaling_analysis" / "stake_scaling_analysis.json",
            input_paths=(live_root / "sessions",),
            optional=True,
        ),
        ArtifactSpec(
            "weekly_drift_rollup_latest",
            "report",
            latest_weekly_rollup,
            input_paths=(analysis / "daily_human_review",),
            optional=True,
        ),
    ]


def inspect_artifact(
    spec: ArtifactSpec,
    *,
    max_hash_bytes: int = DEFAULT_MAX_HASH_BYTES,
    max_jsonl_scan_bytes: int = DEFAULT_MAX_JSONL_SCAN_BYTES,
) -> Dict[str, Any]:
    primary_info = inspect_path(spec.primary_path, max_hash_bytes=max_hash_bytes)
    manifest_info = inspect_path(spec.manifest_path, max_hash_bytes=max_hash_bytes) if spec.manifest_path else None
    row_info = inspect_path(spec.row_path, max_hash_bytes=max_hash_bytes) if spec.row_path else None

    primary_json = _read_json(spec.primary_path) if spec.primary_path.exists() and spec.primary_path.suffix.lower() == ".json" else {}
    manifest_json = (
        _read_json(spec.manifest_path)
        if spec.manifest_path and spec.manifest_path.exists() and spec.manifest_path.suffix.lower() == ".json"
        else {}
    )
    data_for_metadata = primary_json or manifest_json

    row_stats: Dict[str, Any] = {}
    if spec.row_path and spec.row_path.suffix.lower() == ".jsonl":
        scan_limit = (10 * 1024 * 1024 * 1024) if spec.allow_large_row_scan else max_jsonl_scan_bytes
        row_stats = _jsonl_stats(spec.row_path, scan_limit)

    row_count = _extract_row_count(data_for_metadata)
    if row_count is None and row_stats.get("scanned"):
        row_count = _safe_int(row_stats.get("row_count"))
    family_counts = _extract_family_counts(data_for_metadata)
    if not family_counts and row_stats.get("scanned"):
        family_counts = dict(row_stats.get("family_counts") or {})

    max_date = _extract_max_date(data_for_metadata)
    if not max_date and row_stats.get("scanned"):
        max_date = row_stats.get("max_date")

    generated_at = _coerce_generated_at(data_for_metadata)
    if not generated_at and primary_info.get("mtime_utc"):
        generated_at = None

    return {
        "name": spec.name,
        "category": spec.category,
        "optional": spec.optional,
        "notes": spec.notes,
        "primary_path": _path_str(spec.primary_path),
        "manifest_path": _path_str(spec.manifest_path) if spec.manifest_path else None,
        "row_path": _path_str(spec.row_path) if spec.row_path else None,
        "primary": primary_info,
        "manifest": manifest_info,
        "row_file": row_info,
        "generated_at_utc": generated_at,
        "max_date": max_date,
        "row_count": row_count,
        "family_counts": family_counts,
        "row_stats": row_stats,
        "inputs": [],
        "health": {},
    }


def _path_to_artifact_index(artifacts: Sequence[Mapping[str, Any]]) -> Dict[str, Mapping[str, Any]]:
    index: Dict[str, Mapping[str, Any]] = {}
    for art in artifacts:
        for key in ("primary_path", "manifest_path", "row_path"):
            raw = art.get(key)
            if raw:
                index[_norm_path(Path(str(raw)))] = art
    return index


def _input_details(
    input_path: Path,
    artifact_index: Mapping[str, Mapping[str, Any]],
    *,
    max_hash_bytes: int,
    path_info_cache: Dict[str, Dict[str, Any]],
) -> Dict[str, Any]:
    cache_key = _norm_path(input_path)
    info = dict(path_info_cache.get(cache_key) or inspect_path(input_path, max_hash_bytes=max_hash_bytes))
    path_info_cache[cache_key] = dict(info)
    upstream = artifact_index.get(_norm_path(input_path))
    if upstream is not None:
        info["upstream_artifact_name"] = upstream.get("name")
        info["upstream_max_date"] = upstream.get("max_date")
        info["upstream_generated_at_utc"] = upstream.get("generated_at_utc")
    else:
        info["upstream_artifact_name"] = None
        info["upstream_max_date"] = None
        info["upstream_generated_at_utc"] = None
    return info


def attach_lineage_health(
    artifacts: List[Dict[str, Any]],
    specs: Sequence[ArtifactSpec],
    *,
    max_hash_bytes: int = DEFAULT_MAX_HASH_BYTES,
) -> None:
    artifact_index = _path_to_artifact_index(artifacts)
    by_name = {spec.name: spec for spec in specs}
    path_info_cache: Dict[str, Dict[str, Any]] = {}
    mtime_cache: Dict[str, Optional[float]] = {}

    def cached_mtime(path: Path) -> Optional[float]:
        key = _norm_path(path)
        if key not in mtime_cache:
            mtime_cache[key] = _mtime_timestamp(path)
        return mtime_cache[key]

    for artifact in artifacts:
        spec = by_name[artifact["name"]]
        inputs = [
            _input_details(
                path,
                artifact_index,
                max_hash_bytes=max_hash_bytes,
                path_info_cache=path_info_cache,
            )
            for path in spec.input_paths
        ]
        artifact["inputs"] = inputs

        output_mtime = cached_mtime(spec.primary_path)
        input_mtimes = [mt for mt in (cached_mtime(path) for path in spec.input_paths) if mt is not None]
        newest_input_mtime = max(input_mtimes) if input_mtimes else None
        newest_input = None
        if newest_input_mtime is not None:
            for path in spec.input_paths:
                mt = cached_mtime(path)
                if mt is not None and abs(mt - newest_input_mtime) < 0.0001:
                    newest_input = _path_str(path)
                    break

        missing_inputs = [item["path"] for item in inputs if not item.get("exists")]
        missing_required_output = (not artifact["primary"].get("exists")) and not spec.optional
        stale_by_mtime = (
            output_mtime is not None
            and newest_input_mtime is not None
            and output_mtime + 1.0 < newest_input_mtime
        )

        input_max_dates = [
            str(item.get("upstream_max_date"))
            for item in inputs
            if item.get("upstream_max_date")
        ]
        latest_input_max_date = max(input_max_dates) if input_max_dates else None
        artifact_max_date = artifact.get("max_date")
        stale_by_max_date = (
            bool(artifact_max_date)
            and bool(latest_input_max_date)
            and str(artifact_max_date) < str(latest_input_max_date)
        )

        warnings: List[str] = []
        errors: List[str] = []
        if missing_required_output:
            errors.append("required_primary_path_missing")
        elif not artifact["primary"].get("exists"):
            warnings.append("optional_primary_path_missing")
        if missing_inputs:
            warnings.append("missing_inputs")
        if stale_by_mtime:
            warnings.append("output_older_than_newest_input_mtime")
        if stale_by_max_date:
            warnings.append("artifact_max_date_lags_upstream_max_date")
        if artifact["primary"].get("exists") and artifact.get("generated_at_utc") is None and spec.category != "source":
            warnings.append("generated_at_utc_missing")
        if artifact["primary"].get("exists") and artifact.get("max_date") is None and spec.category in {"table", "model", "report"}:
            warnings.append("max_date_missing_or_uninferable")

        status = "error" if errors else ("warning" if warnings else "ok")
        artifact["health"] = {
            "status": status,
            "warnings": warnings,
            "errors": errors,
            "missing_inputs": missing_inputs,
            "output_mtime_utc": _iso_from_timestamp(output_mtime),
            "newest_input_mtime_utc": _iso_from_timestamp(newest_input_mtime),
            "newest_input_path": newest_input,
            "stale_by_mtime": stale_by_mtime,
            "artifact_max_date": artifact_max_date,
            "latest_upstream_max_date": latest_input_max_date,
            "stale_by_max_date": stale_by_max_date,
        }


def build_report(
    *,
    project_dir: Path = PROJECT_DIR,
    specs: Optional[Sequence[ArtifactSpec]] = None,
    max_hash_bytes: int = DEFAULT_MAX_HASH_BYTES,
    max_jsonl_scan_bytes: int = DEFAULT_MAX_JSONL_SCAN_BYTES,
) -> Dict[str, Any]:
    specs = list(specs or canonical_artifact_specs(project_dir))
    artifacts = [
        inspect_artifact(
            spec,
            max_hash_bytes=max_hash_bytes,
            max_jsonl_scan_bytes=max_jsonl_scan_bytes,
        )
        for spec in specs
    ]
    attach_lineage_health(artifacts, specs, max_hash_bytes=max_hash_bytes)

    counts = {
        "artifacts_total": len(artifacts),
        "ok": sum(1 for art in artifacts if art["health"]["status"] == "ok"),
        "warning": sum(1 for art in artifacts if art["health"]["status"] == "warning"),
        "error": sum(1 for art in artifacts if art["health"]["status"] == "error"),
        "missing_required": sum(
            1 for art in artifacts if "required_primary_path_missing" in art["health"].get("errors", [])
        ),
        "stale_by_mtime": sum(1 for art in artifacts if art["health"].get("stale_by_mtime")),
        "stale_by_max_date": sum(1 for art in artifacts if art["health"].get("stale_by_max_date")),
    }
    status = "error" if counts["error"] else ("warning" if counts["warning"] else "ok")
    return {
        "schema_version": 1,
        "generated_at_utc": _now_iso(),
        "project_dir": _path_str(project_dir),
        "status": status,
        "summary": counts,
        "artifacts": artifacts,
    }


def _short_path(path: Any, project_dir: Path) -> str:
    if not path:
        return ""
    try:
        return str(Path(str(path)).resolve().relative_to(project_dir.resolve()))
    except Exception:
        return str(path)


def render_markdown(report: Mapping[str, Any]) -> str:
    project_dir = Path(str(report.get("project_dir") or PROJECT_DIR))
    summary = report.get("summary") or {}
    lines: List[str] = [
        "# Artifact Lineage/Freshness Report",
        "",
        f"- Generated: `{report.get('generated_at_utc')}`",
        f"- Status: **{str(report.get('status') or 'unknown').upper()}**",
        (
            f"- Artifacts: {summary.get('artifacts_total', 0)} total, "
            f"{summary.get('ok', 0)} ok, {summary.get('warning', 0)} warning, "
            f"{summary.get('error', 0)} error"
        ),
        (
            f"- Staleness: {summary.get('stale_by_mtime', 0)} older-than-input, "
            f"{summary.get('stale_by_max_date', 0)} max-date lag"
        ),
        "",
    ]

    actionable = [
        art for art in report.get("artifacts", [])
        if (art.get("health") or {}).get("status") in {"warning", "error"}
    ]
    if actionable:
        lines.append("## Actionable Items")
        lines.append("")
        for art in actionable:
            health = art.get("health") or {}
            tags = list(health.get("errors") or []) + list(health.get("warnings") or [])
            lines.append(
                f"- **{art.get('name')}** [{health.get('status')}]: "
                + ", ".join(tags)
            )
            if health.get("stale_by_mtime"):
                lines.append(
                    f"  - output `{health.get('output_mtime_utc')}` is older than "
                    f"`{_short_path(health.get('newest_input_path'), project_dir)}` "
                    f"at `{health.get('newest_input_mtime_utc')}`"
                )
            if health.get("stale_by_max_date"):
                lines.append(
                    f"  - artifact max_date `{health.get('artifact_max_date')}` lags "
                    f"upstream `{health.get('latest_upstream_max_date')}`"
                )
        lines.append("")

    lines.append("## Artifact Summary")
    lines.append("")
    lines.append("| artifact | status | max_date | rows | families | primary |")
    lines.append("|---|---:|---:|---:|---|---|")
    for art in report.get("artifacts", []):
        health = art.get("health") or {}
        families = art.get("family_counts") or {}
        family_text = ", ".join(f"{k}:{v}" for k, v in sorted(families.items())) if families else ""
        lines.append(
            "| "
            + " | ".join(
                [
                    str(art.get("name")),
                    str(health.get("status")),
                    str(art.get("max_date") or ""),
                    str(art.get("row_count") if art.get("row_count") is not None else ""),
                    family_text,
                    f"`{_short_path(art.get('primary_path'), project_dir)}`",
                ]
            )
            + " |"
        )
    lines.append("")
    return "\n".join(lines)


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _write_csv(path: Path, report: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "name",
        "category",
        "status",
        "max_date",
        "row_count",
        "family_counts_json",
        "primary_path",
        "generated_at_utc",
        "stale_by_mtime",
        "stale_by_max_date",
        "warnings",
        "errors",
    ]
    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for art in report.get("artifacts", []):
            health = art.get("health") or {}
            writer.writerow(
                {
                    "name": art.get("name"),
                    "category": art.get("category"),
                    "status": health.get("status"),
                    "max_date": art.get("max_date"),
                    "row_count": art.get("row_count"),
                    "family_counts_json": json.dumps(art.get("family_counts") or {}, sort_keys=True),
                    "primary_path": art.get("primary_path"),
                    "generated_at_utc": art.get("generated_at_utc"),
                    "stale_by_mtime": health.get("stale_by_mtime"),
                    "stale_by_max_date": health.get("stale_by_max_date"),
                    "warnings": ";".join(health.get("warnings") or []),
                    "errors": ";".join(health.get("errors") or []),
                }
            )


def write_report(report: Mapping[str, Any], output_root: Path, output_stem: str) -> Dict[str, str]:
    output_root.mkdir(parents=True, exist_ok=True)
    json_path = output_root / f"{output_stem}.json"
    md_path = output_root / f"{output_stem}.md"
    csv_path = output_root / f"{output_stem}.csv"
    _write_json(json_path, report)
    _write_text(md_path, render_markdown(report))
    _write_csv(csv_path, report)
    return {
        "json_path": _path_str(json_path),
        "markdown_path": _path_str(md_path),
        "csv_path": _path_str(csv_path),
    }


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--project-dir", type=Path, default=PROJECT_DIR)
    p.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    p.add_argument("--output-stem", default=DEFAULT_OUTPUT_STEM)
    p.add_argument(
        "--max-hash-mb",
        type=float,
        default=DEFAULT_MAX_HASH_BYTES / (1024 * 1024),
        help="Maximum file size to content-hash. Larger files get mtime/size only.",
    )
    p.add_argument(
        "--max-jsonl-scan-mb",
        type=float,
        default=DEFAULT_MAX_JSONL_SCAN_BYTES / (1024 * 1024),
        help="Maximum JSONL row file size to scan for fallback row/date/family stats.",
    )
    p.add_argument("--strict", action="store_true", help="Exit non-zero when any required artifact is missing.")
    return p.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    report = build_report(
        project_dir=args.project_dir,
        max_hash_bytes=int(args.max_hash_mb * 1024 * 1024),
        max_jsonl_scan_bytes=int(args.max_jsonl_scan_mb * 1024 * 1024),
    )
    paths = write_report(report, args.output_root, args.output_stem)
    print(f"Wrote {paths['json_path']}")
    print(f"Wrote {paths['markdown_path']}")
    print(f"Wrote {paths['csv_path']}")
    if args.strict and report.get("summary", {}).get("error", 0):
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
