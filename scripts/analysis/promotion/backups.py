"""Atomic file copy + backup archive machinery for file-swap promotions.

Used by Stage-1, Stage-2, Stage-3-v2 promote/demote commands. Each
promotion writes a single backup at `<file>.prior_promote.json` (rolling
latest that demote restores from), and rotates any prior backup into a
sibling `<file>.prior_promote_archive/` directory under a timestamped
filename, GC'd to the most-recent BACKUP_ARCHIVE_KEEP entries.
"""
from __future__ import annotations

import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import constants as _constants
from .events import _now_iso


def _atomic_copy(src: Path, dst: Path) -> None:
    """Atomic on-disk swap: write to a temp sibling, then os.replace.
    Survives a crash mid-promotion without leaving a partial dst file.
    Used for Stage-2 staging -> production cache."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_suffix(dst.suffix + ".promote_tmp")
    shutil.copy2(src, tmp)
    os.replace(tmp, dst)


def _capture_artifact_lineage(artifact_path: Path) -> Optional[Dict[str, Any]]:
    """Active #16 (2026-05-17): pull the `lineage` block off an
    artifact JSON if present. Used by promote.py to forward the
    source artifact's build-time lineage onto the audit row.

    Best-effort: a missing file, malformed JSON, or absent lineage
    block all return None. We must never let a missing lineage
    field block a real promotion.
    """
    if not artifact_path.exists():
        return None
    try:
        payload = json.loads(artifact_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    lineage = payload.get("lineage")
    if isinstance(lineage, dict):
        return lineage
    return None


def _compute_promotion_lineage() -> Dict[str, Any]:
    """Active #16 helper: stamp a fresh `promotion_lineage` block at
    promotion time (current git_sha + timestamp). Forwarded onto the
    audit row alongside `source_artifact_lineage`. Returns a minimal
    dict on import failure so the audit row never lacks the field
    entirely."""
    try:
        from scripts.analysis.artifact_lineage import promotion_lineage as _pl
    except ImportError:
        try:
            from artifact_lineage import promotion_lineage as _pl  # type: ignore[no-redef]
        except ImportError:
            return {"schema_version": 1, "promoted_at_utc": _now_iso()}
    return _pl(project_root=_constants.PROJECT_DIR)


def _backup_path(prod_path: Path) -> Path:
    """The conventional location for the pre-promotion backup of a
    production file. Symmetric across all file-swap levers so the
    demotion code knows where to look."""
    return prod_path.with_suffix(prod_path.suffix + ".prior_promote.json")


def _archive_dir_for(prod_path: Path) -> Path:
    """Sibling directory holding the timestamped archive of prior
    backups (one entry per past promotion, GC'd to the most recent
    BACKUP_ARCHIVE_KEEP)."""
    return prod_path.parent / (prod_path.name + ".prior_promote_archive")


def _list_archive_backups(prod_path: Path) -> List[Path]:
    """Return existing archive backups sorted by mtime ASC
    (oldest first)."""
    archive = _archive_dir_for(prod_path)
    if not archive.exists() or not archive.is_dir():
        return []
    entries: List[Path] = []
    for p in archive.iterdir():
        if p.is_file() and p.suffix == ".json":
            entries.append(p)
    entries.sort(key=lambda p: p.stat().st_mtime)
    return entries


def _gc_archive_backups(
    prod_path: Path, *, keep: Optional[int] = None,
) -> List[Path]:
    """Trim the archive directory to the `keep` most-recent files.
    Returns the list of deleted paths (oldest first)."""
    if keep is None:
        keep = _constants.BACKUP_ARCHIVE_KEEP
    if keep < 0:
        keep = 0
    entries = _list_archive_backups(prod_path)
    if len(entries) <= keep:
        return []
    to_delete = entries[: len(entries) - keep]
    deleted: List[Path] = []
    for p in to_delete:
        try:
            p.unlink()
            deleted.append(p)
        except OSError:
            # Best-effort GC: failures don't block the promotion path.
            continue
    return deleted


def _rotate_existing_backup_to_archive(prod_path: Path) -> Optional[Path]:
    """If `<file>.prior_promote.json` already exists, move it into
    the archive directory under a timestamped filename so the new
    backup can be written to the canonical path without losing the
    prior one. Returns the archive path on success, None when no
    prior backup existed.
    """
    current_backup = _backup_path(prod_path)
    if not current_backup.exists():
        return None
    archive = _archive_dir_for(prod_path)
    archive.mkdir(parents=True, exist_ok=True)
    # Timestamp the archive file by the prior backup's mtime so the
    # archive preserves "when this backup was originally captured"
    # rather than "when it was archived." Format compact + sortable.
    try:
        mtime = current_backup.stat().st_mtime
    except OSError:
        mtime = None
    if mtime is not None:
        stamp = datetime.fromtimestamp(
            mtime, tz=timezone.utc,
        ).strftime("%Y%m%dT%H%M%SZ")
    else:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target = archive / f"{stamp}.json"
    # Disambiguate if the same-second timestamp collides with an
    # existing archive file (could happen on a fast-rerun test).
    suffix = 0
    while target.exists():
        suffix += 1
        target = archive / f"{stamp}_{suffix}.json"
    try:
        os.replace(current_backup, target)
    except OSError:
        # If rename fails (different filesystems, permission, etc.),
        # fall back to copy-then-unlink. Best-effort; promotion path
        # MUST NOT block on backup-archive failure.
        try:
            shutil.copy2(current_backup, target)
            current_backup.unlink()
        except OSError:
            return None
    return target


def _backup_prior_production(prod_path: Path) -> Optional[Path]:
    """Atomically copy the current production file to its backup
    location BEFORE a swap. Returns the backup path on success, None
    when the production file doesn't exist (first-promotion case --
    demotion will revert by deleting the new production file).

    Atomic-write pattern matches `_atomic_copy` so a crash mid-backup
    can't leave a partial backup that demotion would then trust.

    Active #14 (2026-05-17): before writing the new backup, rotate
    any EXISTING backup into the sibling archive directory + GC the
    archive to BACKUP_ARCHIVE_KEEP most-recent entries. This gives
    the operator multi-promotion rollback history without breaking
    the existing "latest backup at .prior_promote.json" contract
    demote relies on.
    """
    if not prod_path.exists():
        return None
    backup = _backup_path(prod_path)
    backup.parent.mkdir(parents=True, exist_ok=True)
    # Rotate existing backup into archive (best-effort; failure does
    # not block the new backup write).
    try:
        _rotate_existing_backup_to_archive(prod_path)
    except Exception:  # noqa: BLE001
        pass
    tmp = backup.with_suffix(backup.suffix + ".backup_tmp")
    shutil.copy2(prod_path, tmp)
    os.replace(tmp, backup)
    # GC the archive AFTER the new backup is safely on disk so a
    # crash mid-GC can never leave the operator with too few
    # backups + no new one.
    try:
        _gc_archive_backups(prod_path)
    except Exception:  # noqa: BLE001
        pass
    return backup
