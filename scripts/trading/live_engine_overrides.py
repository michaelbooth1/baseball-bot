"""live_engine_overrides.py -- runtime config overrides file.

Lets the auto-promote/demote daemon actuate CLI-flag levers
(stake-scaling, gate-threshold) the same way file-swap levers
(stage2, stage3-v2) are already actuated: by mutating a file the
live engine reads on startup.

Without this layer the daemon could only PRINT a recommended CLI
flag change and rely on the operator typing it into their saved
command. The daemon then had to skip those two of four levers with
`skipped_cli_flag_lever`. This module closes that gap.

Precedence rules (applied at end of `parse_live_args`):
  1. Explicit CLI flag (highest -- operator wins)
  2. Override file value
  3. argparse default (lowest)

We detect "operator passed this flag" by scanning argv for the long-flag
tokens; if a key's flag is in argv, the override file is ignored for
that key.

Schema (`cache/live_engine_overrides.json`):

    {
      "version": 1,
      "calibrated_stake_scale_mode": "enforce",
      "gate_thresholds": {
        "gate_extreme_edge": 0.22,
        "gate_min_edge": 0.10,
        "gate_min_inning": 5,
        "gate_min_entry_ask": 0.55,
        "gate_runs_needed_max": 4.0
      },
      "_audit": {
        "last_modified_utc": "2026-05-16T18:30:00Z",
        "last_modified_by": "auto_daemon",
        "last_promotion_event_at": "..."
      }
    }

All top-level keys except `version` are optional. Keys not present
in the file fall through to argparse defaults.

Backup pattern mirrors stage2/stage3-v2 promotion: pre-write backup
to `<file>.prior_promote.json` so demotion can roll back without
guessing what the prior value was.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple


PROJECT_DIR = Path(__file__).resolve().parents[2]

DEFAULT_OVERRIDES_PATH = PROJECT_DIR / "cache" / "live_engine_overrides.json"

OVERRIDES_SCHEMA_VERSION = 1


# Mapping from override-file key -> (namespace, attr_name, cli_flag).
# `namespace` is "live" (live_args) or "trade" (trade_args from
# signal_engine). `cli_flag` is the long-form argparse flag we scan
# argv for; if it appears we skip the override (operator wins).
#
# Gate thresholds live under the nested `gate_thresholds` block; their
# routing is in _GATE_THRESHOLD_ROUTES below.
_TOP_LEVEL_ROUTES: Dict[str, Tuple[str, str, str]] = {
    "calibrated_stake_scale_mode": (
        "live", "calibrated_stake_scale_mode", "--calibrated-stake-scale-mode",
    ),
    # 2026-06-03: Alt A (empirical-when-available) runtime-shadow
    # promotion. Setting this to "shadow" makes SignalEngine compute
    # fair_value_alt_empirical alongside the production fair_value on
    # every candidate, so the offline shadow-override report keeps
    # accumulating evidence per-tick without any decision change. The
    # daily review has been recommending this lever since 2026-05-27
    # (alt_a_empirical_when_available: promote_to_runtime_shadow). The
    # scope-mode lever (--stage1-alt-a-scope-mode) is already at
    # `enforce` per the 2026-05-22 promotion event and already excludes
    # the regressing inning>=8 cohort, so this shadow flip is the only
    # remaining step before any future enforce promotion.
    "stage1_shadow_empirical_mode": (
        "trade",
        "stage1_shadow_empirical_mode",
        "--stage1-shadow-empirical-mode",
    ),
}

_GATE_THRESHOLD_ROUTES: Dict[str, Tuple[str, str, str]] = {
    "gate_extreme_edge":     ("trade", "extreme_edge_max",   "--extreme-edge-max"),
    "gate_min_edge":         ("trade", "edge_threshold",     "--edge-threshold"),
    "gate_min_inning":       ("trade", "min_inning",         "--min-inning"),
    "gate_min_entry_ask":    ("trade", "min_entry_ask",      "--min-entry-ask"),
    "gate_runs_needed_max":  ("trade", "runs_needed_max",    "--runs-needed-max"),
}


def load_overrides(path: Path = DEFAULT_OVERRIDES_PATH) -> Dict[str, Any]:
    """Read the overrides file. Returns {} for missing/malformed/wrong-version.
    Logs a single WARNING line to stderr on malformed JSON or unknown version.
    """
    if not path.exists():
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        print(
            f"WARNING live_engine_overrides: failed to read {path}: {exc!r}; "
            "ignoring overrides.",
            file=sys.stderr,
        )
        return {}
    if not isinstance(data, dict):
        print(
            f"WARNING live_engine_overrides: {path} is not a JSON object; "
            "ignoring overrides.",
            file=sys.stderr,
        )
        return {}
    version = data.get("version")
    if version != OVERRIDES_SCHEMA_VERSION:
        print(
            f"WARNING live_engine_overrides: {path} has version={version!r}, "
            f"expected {OVERRIDES_SCHEMA_VERSION}; ignoring overrides.",
            file=sys.stderr,
        )
        return {}
    return data


def passed_flags(argv: Optional[Sequence[str]]) -> set:
    """Extract long-form flag tokens from argv. Used to detect when the
    operator explicitly passed a flag that an override file might
    otherwise change."""
    flags: set = set()
    if argv is None:
        argv = sys.argv[1:]
    for tok in argv:
        if not isinstance(tok, str):
            continue
        if tok.startswith("--"):
            # Handle both --flag and --flag=value forms
            name = tok.split("=", 1)[0]
            flags.add(name)
    return flags


def apply_overrides(
    *,
    live_args: argparse.Namespace,
    trade_args: argparse.Namespace,
    overrides: Dict[str, Any],
    passed: Optional[Iterable[str]] = None,
) -> List[str]:
    """Mutate `live_args` and `trade_args` in place with values from
    `overrides`. Skip any key whose corresponding CLI flag is in
    `passed`. Returns a list of human-readable application notes for
    logging (one per applied or skipped override).
    """
    notes: List[str] = []
    passed_set = set(passed or [])

    for key, (namespace, attr, cli_flag) in _TOP_LEVEL_ROUTES.items():
        if key not in overrides:
            continue
        new_value = overrides[key]
        if cli_flag in passed_set:
            notes.append(
                f"override {key}: operator passed {cli_flag}; keeping CLI value"
            )
            continue
        target = live_args if namespace == "live" else trade_args
        old_value = getattr(target, attr, None)
        setattr(target, attr, new_value)
        notes.append(
            f"override {key}: {old_value!r} -> {new_value!r} (from file)"
        )

    gate_block = overrides.get("gate_thresholds") or {}
    if not isinstance(gate_block, dict):
        notes.append(
            "override gate_thresholds: not a JSON object; ignored"
        )
        gate_block = {}
    for key, (namespace, attr, cli_flag) in _GATE_THRESHOLD_ROUTES.items():
        if key not in gate_block:
            continue
        new_value = gate_block[key]
        if cli_flag in passed_set:
            notes.append(
                f"override {key}: operator passed {cli_flag}; keeping CLI value"
            )
            continue
        target = live_args if namespace == "live" else trade_args
        old_value = getattr(target, attr, None)
        setattr(target, attr, new_value)
        notes.append(
            f"override {key}: {old_value!r} -> {new_value!r} (from file)"
        )

    # Warn on unknown top-level keys so a typo in promote.py doesn't
    # silently no-op. Skip the well-known structural keys.
    known_top = set(_TOP_LEVEL_ROUTES) | {"version", "gate_thresholds", "_audit"}
    for key in overrides:
        if key not in known_top:
            notes.append(f"override {key!r}: unknown key, ignored")
    for key in gate_block:
        if key not in _GATE_THRESHOLD_ROUTES:
            notes.append(f"override gate_thresholds.{key!r}: unknown key, ignored")

    return notes


# ---------------------------------------------------------------------------
# Writing (used by promote.py to mutate the file when actuating
# stake-scaling / gate-threshold levers).
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _atomic_write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".write_tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, sort_keys=True)
        f.write("\n")
    os.replace(tmp, path)


def backup_path_for(path: Path = DEFAULT_OVERRIDES_PATH) -> Path:
    """Backup location used pre-write. Same `<file>.prior_promote.json`
    suffix the file-swap levers use, so the audit pattern is uniform."""
    return path.with_suffix(path.suffix + ".prior_promote.json")


def backup_overrides_for_promote(
    path: Path = DEFAULT_OVERRIDES_PATH,
) -> Optional[Path]:
    """Atomically copy the current overrides file to its backup location
    before a write. Returns the backup path on success, None if the file
    didn't exist (first-write case -- demotion can revert by removing
    the override key).
    """
    if not path.exists():
        return None
    backup = backup_path_for(path)
    backup.parent.mkdir(parents=True, exist_ok=True)
    tmp = backup.with_suffix(backup.suffix + ".backup_tmp")
    shutil.copy2(path, tmp)
    os.replace(tmp, backup)
    return backup


def set_override(
    *,
    path: Path = DEFAULT_OVERRIDES_PATH,
    operator: str,
    promotion_event_at: Optional[str] = None,
    top_level: Optional[Dict[str, Any]] = None,
    gate_thresholds: Optional[Dict[str, Any]] = None,
) -> Tuple[Optional[Path], Dict[str, Any]]:
    """Apply a sparse update to the overrides file. Returns
    (backup_path_or_none, new_payload).

    - Existing overrides not mentioned in `top_level` / `gate_thresholds`
      are preserved.
    - Unknown keys raise ValueError (so promote.py callers learn at
      write time instead of silently no-opping at engine startup).
    - Backup-on-write happens once per call; the backup captures the
      pre-update state so demotion has somewhere to restore from.
    """
    if top_level:
        for key in top_level:
            if key not in _TOP_LEVEL_ROUTES:
                raise ValueError(
                    f"unknown top-level override key: {key!r}; "
                    f"valid: {sorted(_TOP_LEVEL_ROUTES)}"
                )
    if gate_thresholds:
        for key in gate_thresholds:
            if key not in _GATE_THRESHOLD_ROUTES:
                raise ValueError(
                    f"unknown gate-threshold key: {key!r}; "
                    f"valid: {sorted(_GATE_THRESHOLD_ROUTES)}"
                )

    backup = backup_overrides_for_promote(path)
    existing = load_overrides(path)
    new_payload: Dict[str, Any] = {"version": OVERRIDES_SCHEMA_VERSION}
    for key in _TOP_LEVEL_ROUTES:
        if key in existing:
            new_payload[key] = existing[key]
    existing_gates = (existing.get("gate_thresholds") or {})
    gate_carry: Dict[str, Any] = {}
    for key in _GATE_THRESHOLD_ROUTES:
        if key in existing_gates:
            gate_carry[key] = existing_gates[key]

    if top_level:
        for key, value in top_level.items():
            new_payload[key] = value
    if gate_thresholds:
        for key, value in gate_thresholds.items():
            gate_carry[key] = value
    if gate_carry:
        new_payload["gate_thresholds"] = gate_carry

    new_payload["_audit"] = {
        "last_modified_utc": _now_iso(),
        "last_modified_by": operator,
        "last_promotion_event_at": promotion_event_at,
    }

    _atomic_write_json(path, new_payload)
    return backup, new_payload


def remove_override(
    *,
    path: Path = DEFAULT_OVERRIDES_PATH,
    operator: str,
    promotion_event_at: Optional[str] = None,
    top_level_keys: Optional[Iterable[str]] = None,
    gate_threshold_keys: Optional[Iterable[str]] = None,
) -> Tuple[Optional[Path], Optional[Dict[str, Any]]]:
    """Remove one or more keys from the overrides file. Returns
    (backup_path_or_none, new_payload_or_none). new_payload is None
    when the file no longer contains any overrides (we delete the file
    in that case so the next engine startup uses pure argparse defaults).

    No-op if the file doesn't exist.
    """
    if not path.exists():
        return None, None
    backup = backup_overrides_for_promote(path)
    existing = load_overrides(path)
    if not existing:
        return backup, None

    new_payload: Dict[str, Any] = {"version": OVERRIDES_SCHEMA_VERSION}
    drop_top = set(top_level_keys or [])
    drop_gates = set(gate_threshold_keys or [])
    for key in _TOP_LEVEL_ROUTES:
        if key in existing and key not in drop_top:
            new_payload[key] = existing[key]
    existing_gates = existing.get("gate_thresholds") or {}
    gate_carry: Dict[str, Any] = {}
    for key in _GATE_THRESHOLD_ROUTES:
        if key in existing_gates and key not in drop_gates:
            gate_carry[key] = existing_gates[key]
    if gate_carry:
        new_payload["gate_thresholds"] = gate_carry

    # If no override remains, delete the file -- a missing file is
    # cleaner than an empty stub.
    has_overrides = any(k in new_payload for k in _TOP_LEVEL_ROUTES) or bool(gate_carry)
    if not has_overrides:
        try:
            path.unlink()
        except OSError as exc:
            print(
                f"WARNING live_engine_overrides: failed to remove {path}: {exc!r}",
                file=sys.stderr,
            )
        return backup, None

    new_payload["_audit"] = {
        "last_modified_utc": _now_iso(),
        "last_modified_by": operator,
        "last_promotion_event_at": promotion_event_at,
    }
    _atomic_write_json(path, new_payload)
    return backup, new_payload


def restore_overrides_from_backup(
    *,
    path: Path = DEFAULT_OVERRIDES_PATH,
) -> Tuple[bool, Optional[Path]]:
    """Restore the overrides file from its backup. Returns
    (restored, backup_path). If no backup exists the function deletes
    the overrides file (revert-to-defaults) and returns (False, None).
    """
    backup = backup_path_for(path)
    if not backup.exists():
        try:
            if path.exists():
                path.unlink()
        except OSError as exc:
            print(
                f"WARNING live_engine_overrides: failed to remove {path}: {exc!r}",
                file=sys.stderr,
            )
        return False, None
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".restore_tmp")
    shutil.copy2(backup, tmp)
    os.replace(tmp, path)
    return True, backup


__all__ = [
    "DEFAULT_OVERRIDES_PATH",
    "OVERRIDES_SCHEMA_VERSION",
    "load_overrides",
    "passed_flags",
    "apply_overrides",
    "backup_path_for",
    "backup_overrides_for_promote",
    "set_override",
    "remove_override",
    "restore_overrides_from_backup",
]
