"""artifact_lineage.py -- Stamp lineage info on built artifacts.

Active priority #16 (2026-05-17). When a promotion is later
demoted (especially via Active #13's fast Wilson-UB check), the
operator's first question is "which artifact failed AND what built
it?" Without lineage, the answer requires git-log archaeology
against approximate timestamps. With lineage, the artifact carries
its own answer.

This module is **build-time** lineage:

  lineage = {
    "schema_version": 1,
    "built_at_utc": "...",
    "builder_path": "scripts/analysis/calibrate_signal_probabilities.py",
    "git_sha": "abc1234567",           # short SHA, repo HEAD
    "git_dirty": False,                # working tree had uncommitted changes
    "git_branch": "main",
    "input_hashes": {                  # sha256 of small input files
        "<path>": "sha256:..."
    },
    "input_dir_summaries": {           # for large input trees
        "<path>": {"n_files": ..., "max_mtime_utc": "...", "min_mtime_utc": "..."},
    },
    "python_version": "3.11.4",
  }

Promote.py later reads the artifact's lineage AND adds a fresh
`promoted_lineage` (git sha + timestamp AT PROMOTION) so the audit
row carries BOTH: what was built and where/when it was promoted.

Pure helper module: no global state, no required I/O. Safe to import
from any builder.
"""

from __future__ import annotations

import hashlib
import logging
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Union


LOGGER = logging.getLogger("artifact_lineage")

LINEAGE_SCHEMA_VERSION = 1
# Truncate sha256 hex digests to keep audit rows compact. 16 hex chars
# = 64 bits of entropy, more than enough to detect changes.
_HASH_PREFIX_LEN = 16


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _git(cmd: List[str], *, cwd: Optional[Path] = None) -> Optional[str]:
    """Best-effort git subprocess. Returns stdout stripped, or None
    on any failure (missing git, not a repo, subprocess error)."""
    try:
        out = subprocess.run(
            ["git", *cmd],
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            timeout=5.0,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return out.stdout.strip() or None


def git_sha(*, cwd: Optional[Path] = None, short: bool = True) -> Optional[str]:
    """Return the current git HEAD SHA. Short by default (12 chars)
    to keep audit rows compact. None when not in a git repo."""
    sha = _git(["rev-parse", "HEAD"], cwd=cwd)
    if sha is None:
        return None
    return sha[:12] if short else sha


def git_branch(*, cwd: Optional[Path] = None) -> Optional[str]:
    """Return the current git branch name, or None when in detached
    HEAD / non-git."""
    branch = _git(["symbolic-ref", "--short", "HEAD"], cwd=cwd)
    if branch:
        return branch
    # Detached HEAD: report the abbrev sha as the "branch"
    sha = git_sha(cwd=cwd, short=True)
    return f"detached@{sha}" if sha else None


def git_is_dirty(*, cwd: Optional[Path] = None) -> Optional[bool]:
    """True when working tree has uncommitted changes; False when
    clean; None when git unavailable."""
    out = _git(["status", "--porcelain"], cwd=cwd)
    if out is None:
        return None
    return bool(out.strip())


def hash_file(path: Path, *, prefix_len: int = _HASH_PREFIX_LEN) -> Optional[str]:
    """sha256 of a single file, truncated to `prefix_len` hex chars
    and prefixed with 'sha256:'. Returns None if file is missing."""
    if not path.exists() or not path.is_file():
        return None
    try:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            # 64 KiB chunks: enough to keep memory tiny, large enough
            # to avoid syscall overhead.
            while True:
                chunk = f.read(65536)
                if not chunk:
                    break
                h.update(chunk)
        return f"sha256:{h.hexdigest()[:prefix_len]}"
    except OSError as exc:
        LOGGER.warning("Failed to hash %s: %s", path, exc)
        return None


def summarize_directory(path: Path) -> Optional[Dict[str, Any]]:
    """Compact summary of a directory tree for inputs too large to
    hash. Returns n_files, min_mtime, max_mtime (all in UTC).
    None when path doesn't exist or has no files.

    Walks recursively; counts ALL regular files (not just one
    extension). For typical use against `data/games/regular` this
    visits ~7k files and completes in <100ms.
    """
    if not path.exists() or not path.is_dir():
        return None
    n_files = 0
    min_mtime: Optional[float] = None
    max_mtime: Optional[float] = None
    for entry in path.rglob("*"):
        if not entry.is_file():
            continue
        n_files += 1
        try:
            mtime = entry.stat().st_mtime
        except OSError:
            continue
        if min_mtime is None or mtime < min_mtime:
            min_mtime = mtime
        if max_mtime is None or mtime > max_mtime:
            max_mtime = mtime
    if n_files == 0:
        return {"n_files": 0, "min_mtime_utc": None, "max_mtime_utc": None}
    return {
        "n_files": n_files,
        "min_mtime_utc": datetime.fromtimestamp(
            min_mtime, tz=timezone.utc,
        ).isoformat().replace("+00:00", "Z"),
        "max_mtime_utc": datetime.fromtimestamp(
            max_mtime, tz=timezone.utc,
        ).isoformat().replace("+00:00", "Z"),
    }


def compute_lineage(
    *,
    builder_path: Union[str, Path],
    input_paths: Iterable[Union[str, Path]] = (),
    input_dir_paths: Iterable[Union[str, Path]] = (),
    project_root: Optional[Path] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Compute a build-time lineage dict.

    `builder_path` is the script that produced the artifact (use
    `__file__` from the builder). Recorded as a project-relative
    path when `project_root` is supplied; otherwise as-is.

    `input_paths` are individual files small enough to hash (training
    tables, source JSONs, etc.).

    `input_dir_paths` are directory trees too large to hash; we
    record a count + mtime summary instead.

    `project_root` lets builders pass `PROJECT_DIR` so absolute
    paths get collapsed to repo-relative for readability + git
    metadata is resolved from the right working tree.

    `extra` allows callers to attach builder-specific metadata
    (e.g. CLI args summary, family count). Merged into the top
    level.

    Best-effort throughout: git failures return None, file misses
    are skipped silently. The lineage dict NEVER raises -- a
    broken stamp is always better than a failed artifact build.
    """
    root = Path(project_root) if project_root else None
    builder_p = Path(builder_path)
    if root is not None:
        try:
            builder_str = builder_p.resolve().relative_to(root.resolve()).as_posix()
        except (ValueError, OSError):
            builder_str = str(builder_p)
    else:
        builder_str = str(builder_p)

    input_hashes: Dict[str, Optional[str]] = {}
    for raw in input_paths:
        p = Path(raw)
        if root is not None:
            try:
                key = p.resolve().relative_to(root.resolve()).as_posix()
            except (ValueError, OSError):
                key = str(p)
        else:
            key = str(p)
        input_hashes[key] = hash_file(p)

    input_dir_summaries: Dict[str, Optional[Dict[str, Any]]] = {}
    for raw in input_dir_paths:
        p = Path(raw)
        if root is not None:
            try:
                key = p.resolve().relative_to(root.resolve()).as_posix()
            except (ValueError, OSError):
                key = str(p)
        else:
            key = str(p)
        input_dir_summaries[key] = summarize_directory(p)

    lineage: Dict[str, Any] = {
        "schema_version": LINEAGE_SCHEMA_VERSION,
        "built_at_utc": _now_iso(),
        "builder_path": builder_str,
        "git_sha": git_sha(cwd=root),
        "git_branch": git_branch(cwd=root),
        "git_dirty": git_is_dirty(cwd=root),
        "input_hashes": input_hashes,
        "input_dir_summaries": input_dir_summaries,
        "python_version": (
            f"{sys.version_info.major}.{sys.version_info.minor}"
            f".{sys.version_info.micro}"
        ),
    }
    if extra:
        # Caller-supplied fields. Don't let extras shadow the
        # canonical structure keys above; collisions stay on the
        # canonical value.
        for k, v in extra.items():
            if k not in lineage:
                lineage[k] = v
    return lineage


def promotion_lineage(*, project_root: Optional[Path] = None) -> Dict[str, Any]:
    """A trimmed lineage stamped at PROMOTION time (not build time).

    The artifact already carries its own build-time lineage; this
    captures the git state at the moment of promotion, which can
    differ (e.g. operator promotes a week-old artifact from a
    different working tree). Recorded on the audit row.
    """
    root = Path(project_root) if project_root else None
    return {
        "schema_version": LINEAGE_SCHEMA_VERSION,
        "promoted_at_utc": _now_iso(),
        "git_sha": git_sha(cwd=root),
        "git_branch": git_branch(cwd=root),
        "git_dirty": git_is_dirty(cwd=root),
        "promoter_hostname": os.uname().nodename if hasattr(os, "uname") else None,
    }


def extract_lineage_from_artifact(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Pull the `lineage` block off an artifact JSON, returning None
    when absent. Used by promote.py to forward source-artifact
    lineage into the audit row."""
    lineage = payload.get("lineage")
    if isinstance(lineage, dict):
        return lineage
    return None
