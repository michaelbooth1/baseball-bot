"""Writers and manifest builder for unified signal outputs."""

from __future__ import annotations

import argparse
import csv
import json
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List

from scripts.analysis.unified_signal_table.schema import SCHEMA_VERSION
from scripts.analysis.unified_signal_table.utils import _now_iso

def write_jsonl(path: Path, rows: List[Dict[str, Any]]) -> None:
    with open(path, "w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row) + "\n")


def write_csv(path: Path, rows: List[Dict[str, Any]], columns: List[str]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def sort_master_rows(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return sorted(
        rows,
        key=lambda r: (
            str(r.get("mode") or ""),
            str(r.get("session_date") or ""),
            str(r.get("placed_at") or ""),
            str(r.get("bet_id") or ""),
        ),
    )


def write_manifest(
    path: Path,
    args: argparse.Namespace,
    master_columns: List[str],
    master_rows: List[Dict[str, Any]],
    order_events_rows: List[Dict[str, Any]],
    snapshot_rows: List[Dict[str, Any]],
    mode_stats: Dict[str, Dict[str, int]],
    warnings: List[str],
    hard_errors: List[str],
) -> None:
    by_mode_counts: Dict[str, int] = defaultdict(int)
    quality_counts: Dict[str, int] = defaultdict(int)

    for row in master_rows:
        by_mode_counts[str(row.get("mode") or "unknown")] += 1
        for flag in [
            "flag_missing_settlement",
            "flag_missing_capture",
            "flag_missing_ledger",
            "flag_settled_with_null_fill_price",
            "flag_profit_nonzero_without_fill",
            "flag_order_status_conflict",
            "flag_capture_token_mismatch",
            "flag_ts_order_invalid",
        ]:
            if row.get(flag):
                quality_counts[flag] += 1

    manifest = {
        "schema_version": SCHEMA_VERSION,
        "generated_at_utc": _now_iso(),
        "config": {
            "mode": args.mode,
            "min_date": args.min_date or None,
            "max_date": args.max_date or None,
            "horizons": args.horizons_list,
            "fill_window_secs": args.fill_window_secs,
            "stage1_support_backfill_enabled": not bool(
                getattr(args, "disable_stage1_support_backfill", False)
            ),
            "stage1_cache_path": (
                str(getattr(args, "stage1_cache_path", ""))
                if getattr(args, "stage1_cache_path", None)
                else None
            ),
            "strict": args.strict,
            "output_root": str(args.output_root),
            "master_columns": master_columns,
        },
        "counts": {
            "signals_master_total": len(master_rows),
            "signals_master_by_mode": dict(by_mode_counts),
            "order_events_total": len(order_events_rows),
            "snapshot_rows_total": len(snapshot_rows),
            "mode_stats": mode_stats,
        },
        "quality_flags": dict(quality_counts),
        "warnings_count": len(warnings),
        "hard_errors_count": len(hard_errors),
        "warnings": warnings[:300],
        "hard_errors": hard_errors[:300],
        "status": "failed" if hard_errors else "ok",
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)

