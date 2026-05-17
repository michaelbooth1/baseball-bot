#!/usr/bin/env python3
"""
Build a unified event-level signal training table.

This file is intentionally kept as a thin CLI/orchestration layer. The heavy
helpers live under scripts/analysis/unified_signal_table/:
- schema.py: column groups and CaptureData
- loaders.py: sessions, ledgers, candidate rows, captures
- snapshot_features.py: horizon snapshots and simulated fill features
- row_builder.py: canonical row assembly and quality flags
- writers.py: JSONL/CSV/manifest writers
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


PROJECT_DIR = Path(__file__).resolve().parents[2]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from scripts.analysis.unified_signal_table.schema import (  # noqa: E402
    BASE_MASTER_COLUMNS,
    ORDER_EVENT_COLUMNS,
    PARAM_COLUMNS,
    PARAM_KEYS_COMMON,
    PARAM_KEYS_LIVE,
    PHASE2_STATIC_COLUMNS,
    SCHEMA_VERSION,
    SNAPSHOT_COLUMNS,
    CaptureData,
    _build_master_columns,
)
from scripts.analysis.unified_signal_table.utils import (  # noqa: E402
    _best_event_time,
    _coalesce,
    _date_in_range,
    _infer_session_date,
    _now_iso,
    _parse_iso_to_epoch,
    _read_json,
    _read_jsonl,
    _safe_bool,
    _safe_float,
    _safe_int,
)
from scripts.analysis.unified_signal_table.loaders import (  # noqa: E402
    load_candidate_trade_rows_for_mode,
    load_captures_for_mode,
    load_ledger_events_for_mode,
    load_sessions_for_mode,
)
from scripts.analysis.unified_signal_table.snapshot_features import (  # noqa: E402
    _build_snapshot_rows,
    _compute_phase2_capture_features,
    _extract_book_levels,
)
from scripts.analysis.unified_signal_table.row_builder import (  # noqa: E402
    _build_order_events_rows,
    _build_state_value_fields,
    _canonical_value,
    _extract_param_values,
    _state_value_value,
    _timestamp_order_invalid,
    build_master_rows_for_mode,
)
from scripts.analysis.unified_signal_table.writers import (  # noqa: E402
    sort_master_rows,
    write_csv,
    write_jsonl,
    write_manifest,
)
from scripts.analysis.build_calibration_opportunity_training_table import (  # noqa: E402
    DEFAULT_STAGE1_CACHE_PATH,
    _load_stage1_cache_cells,
    backfill_stage1_support_fields,
)

DEFAULT_OUTPUT_ROOT = PROJECT_DIR / "data" / "analysis_output" / "unified_signals"

LIVE_SESSIONS_ROOT = PROJECT_DIR / "data" / "live_trading" / "sessions"
LIVE_LEDGER_PATH = PROJECT_DIR / "data" / "live_trading" / "master_ledger.jsonl"
LIVE_CAPTURES_ROOT = PROJECT_DIR / "data" / "live_trading" / "book_captures"
LIVE_CANDIDATES_ROOT = PROJECT_DIR / "data" / "live_trading" / "candidate_universe"

PAPER_SESSIONS_ROOT = PROJECT_DIR / "data" / "paper_trading" / "sessions"
PAPER_LEDGER_PATH = PROJECT_DIR / "data" / "paper_trading" / "master_ledger.jsonl"
PAPER_CAPTURES_ROOT = PROJECT_DIR / "data" / "paper_trading" / "book_captures"
PAPER_CANDIDATES_ROOT = PROJECT_DIR / "data" / "paper_trading" / "candidate_universe"

DEFAULT_HORIZONS = [1, 2, 5, 10, 30]
DEFAULT_HORIZONS_STR = ",".join(str(h) for h in DEFAULT_HORIZONS)
DEFAULT_FILL_WINDOW_SECS = 30

LOGGER = logging.getLogger("build_unified_signal_table")

def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Build unified event-level signal training tables.")
    p.add_argument(
        "--mode",
        choices=["live", "paper", "both"],
        default="both",
        help="Dataset mode to build (default: both).",
    )
    p.add_argument("--min-date", type=str, default="", help="Inclusive lower date bound (YYYY-MM-DD).")
    p.add_argument("--max-date", type=str, default="", help="Inclusive upper date bound (YYYY-MM-DD).")
    p.add_argument(
        "--output-root",
        type=Path,
        default=DEFAULT_OUTPUT_ROOT,
        help=f"Output directory (default: {DEFAULT_OUTPUT_ROOT}).",
    )
    p.add_argument(
        "--horizons",
        type=str,
        default=DEFAULT_HORIZONS_STR,
        help=f"Comma-separated horizon seconds for ask/bid features (default: {DEFAULT_HORIZONS_STR}).",
    )
    p.add_argument(
        "--fill-window-secs",
        type=int,
        default=DEFAULT_FILL_WINDOW_SECS,
        help=f"Window in seconds for simulated fill features (default: {DEFAULT_FILL_WINDOW_SECS}).",
    )
    p.add_argument(
        "--stage1-cache-path",
        type=Path,
        default=DEFAULT_STAGE1_CACHE_PATH,
        help=(
            "Stage-1 cache used only to backfill support/trust diagnostics from "
            "logged state-cell keys (default: cache/mlb_ou_cache.json)."
        ),
    )
    p.add_argument(
        "--disable-stage1-support-backfill",
        action="store_true",
        help="Do not backfill Stage-1 support diagnostics from cached state-cell keys.",
    )
    p.add_argument("--strict", action="store_true", help="Fail build if hard checks fail.")
    p.add_argument("--verbose", action="store_true", help="Verbose logging.")
    return p.parse_args(argv)

def _parse_horizons_csv(raw: str) -> List[int]:
    parts = [x.strip() for x in str(raw or "").split(",")]
    vals: List[int] = []
    for p in parts:
        if not p:
            continue
        try:
            v = int(p)
        except Exception as exc:
            raise SystemExit(f"Invalid --horizons value '{p}': {exc}") from exc
        if v <= 0:
            raise SystemExit("--horizons values must be positive integers")
        vals.append(v)
    uniq = sorted(set(vals))
    if not uniq:
        raise SystemExit("--horizons must contain at least one positive integer")
    return uniq

def build_for_mode(
    mode: str,
    min_date: Optional[str],
    max_date: Optional[str],
    horizons: List[int],
    fill_window_secs: int,
    master_columns: List[str],
    warnings: List[str],
    hard_errors: List[str],
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, Any]], Dict[str, int]]:
    if mode == "live":
        sessions_root = LIVE_SESSIONS_ROOT
        ledger_path = LIVE_LEDGER_PATH
        captures_root = LIVE_CAPTURES_ROOT
        candidates_root = LIVE_CANDIDATES_ROOT
    elif mode == "paper":
        sessions_root = PAPER_SESSIONS_ROOT
        ledger_path = PAPER_LEDGER_PATH
        captures_root = PAPER_CAPTURES_ROOT
        candidates_root = PAPER_CANDIDATES_ROOT
    else:
        raise ValueError(f"Unsupported mode: {mode}")

    session_rows = load_sessions_for_mode(
        mode=mode,
        sessions_root=sessions_root,
        min_date=min_date,
        max_date=max_date,
        warnings=warnings,
        hard_errors=hard_errors,
    )
    ledger_events = load_ledger_events_for_mode(mode=mode, ledger_path=ledger_path, warnings=warnings)
    captures = load_captures_for_mode(
        mode=mode,
        captures_root=captures_root,
        min_date=min_date,
        max_date=max_date,
        warnings=warnings,
        hard_errors=hard_errors,
    )
    candidate_rows = load_candidate_trade_rows_for_mode(
        mode=mode,
        candidates_root=candidates_root,
        min_date=min_date,
        max_date=max_date,
        warnings=warnings,
        hard_errors=hard_errors,
    )

    master_rows = build_master_rows_for_mode(
        mode=mode,
        session_rows=session_rows,
        ledger_events=ledger_events,
        captures=captures,
        candidate_rows=candidate_rows,
        min_date=min_date,
        max_date=max_date,
        horizons=horizons,
        fill_window_secs=fill_window_secs,
        master_columns=master_columns,
        warnings=warnings,
        hard_errors=hard_errors,
    )
    order_events_rows = _build_order_events_rows(mode=mode, ledger_events=ledger_events)
    snapshot_rows = _build_snapshot_rows(mode=mode, captures=captures)

    stats = {
        "session_bets": len(session_rows),
        "ledger_bet_ids": len(ledger_events),
        "capture_bet_ids": len(captures),
        "candidate_trade_bet_ids": len(candidate_rows),
        "master_rows": len(master_rows),
        "order_events_rows": len(order_events_rows),
        "snapshot_rows": len(snapshot_rows),
    }
    return master_rows, order_events_rows, snapshot_rows, stats

def main(argv: Optional[List[str]] = None) -> None:
    args = parse_args(argv)

    if args.min_date:
        datetime.strptime(args.min_date, "%Y-%m-%d")
    if args.max_date:
        datetime.strptime(args.max_date, "%Y-%m-%d")
    if args.min_date and args.max_date and args.min_date > args.max_date:
        raise SystemExit("--min-date must be <= --max-date")
    if args.fill_window_secs <= 0:
        raise SystemExit("--fill-window-secs must be > 0")

    args.horizons_list = _parse_horizons_csv(args.horizons)
    master_columns = _build_master_columns(args.horizons_list, args.fill_window_secs)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s %(message)s",
    )

    args.output_root.mkdir(parents=True, exist_ok=True)

    modes = ["live", "paper"] if args.mode == "both" else [args.mode]
    warnings: List[str] = []
    hard_errors: List[str] = []

    all_master_rows: List[Dict[str, Any]] = []
    all_order_events_rows: List[Dict[str, Any]] = []
    all_snapshot_rows: List[Dict[str, Any]] = []
    mode_stats: Dict[str, Dict[str, int]] = {}

    for mode in modes:
        LOGGER.info("Building mode=%s", mode)
        m_rows, e_rows, s_rows, stats = build_for_mode(
            mode=mode,
            min_date=args.min_date or None,
            max_date=args.max_date or None,
            horizons=args.horizons_list,
            fill_window_secs=args.fill_window_secs,
            master_columns=master_columns,
            warnings=warnings,
            hard_errors=hard_errors,
        )
        mode_stats[mode] = stats
        all_master_rows.extend(m_rows)
        all_order_events_rows.extend(e_rows)
        all_snapshot_rows.extend(s_rows)
        LOGGER.info(
            "mode=%s rows: master=%d events=%d snapshots=%d",
            mode,
            len(m_rows),
            len(e_rows),
            len(s_rows),
        )

    # Deduplicate hard errors for readability
    hard_errors = sorted(set(hard_errors))

    all_master_rows = sort_master_rows(all_master_rows)
    all_order_events_rows = sorted(
        all_order_events_rows,
        key=lambda r: (str(r.get("mode") or ""), str(r.get("bet_id") or ""), int(r.get("event_seq") or 0)),
    )
    all_snapshot_rows = sorted(
        all_snapshot_rows,
        key=lambda r: (
            str(r.get("mode") or ""),
            str(r.get("bet_id") or ""),
            int(r.get("seq") or 0),
        ),
    )
    if not bool(args.disable_stage1_support_backfill):
        stage1_cells = _load_stage1_cache_cells(args.stage1_cache_path, warnings)
        mode_stats["_stage1_support_backfill"] = backfill_stage1_support_fields(
            all_master_rows,
            cells=stage1_cells,
        )

    master_jsonl_path = args.output_root / "signals_master.jsonl"
    master_csv_path = args.output_root / "signals_master.csv"
    events_path = args.output_root / "signal_order_events.jsonl"
    snapshots_path = args.output_root / "signal_book_snapshots.jsonl"
    manifest_path = args.output_root / "build_manifest.json"

    write_jsonl(master_jsonl_path, all_master_rows)
    write_csv(master_csv_path, all_master_rows, master_columns)
    write_jsonl(events_path, all_order_events_rows)
    write_jsonl(snapshots_path, all_snapshot_rows)
    write_manifest(
        path=manifest_path,
        args=args,
        master_columns=master_columns,
        master_rows=all_master_rows,
        order_events_rows=all_order_events_rows,
        snapshot_rows=all_snapshot_rows,
        mode_stats=mode_stats,
        warnings=warnings,
        hard_errors=hard_errors,
    )

    LOGGER.info("Wrote %s", master_jsonl_path)
    LOGGER.info("Wrote %s", master_csv_path)
    LOGGER.info("Wrote %s", events_path)
    LOGGER.info("Wrote %s", snapshots_path)
    LOGGER.info("Wrote %s", manifest_path)
    LOGGER.info("Warnings: %d  Hard errors: %d", len(warnings), len(hard_errors))

    if args.strict and hard_errors:
        raise SystemExit("Strict mode failed: hard checks did not pass. See build_manifest.json.")


if __name__ == "__main__":
    main()
