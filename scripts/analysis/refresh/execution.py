"""Step execution + staleness check + output tailing.

The runtime side of the refresh engine: how each RefreshStep gets
turned into a RefreshStepResult, including the skip-if-fresh policy.
"""
from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path
from typing import Iterable, Optional, Tuple

from . import config as _config
from .config import (
    RefreshConfig,
    RefreshStep,
    RefreshStepResult,
    StalenessCheck,
)
from .preflight import INLINE_HANDLERS


def _output_tail(output: str, max_chars: int = 4000) -> str:
    output = output.strip()
    if len(output) <= max_chars:
        return output
    return output[-max_chars:]


def _run_inline_step(step: RefreshStep, config: RefreshConfig) -> RefreshStepResult:
    handler = INLINE_HANDLERS.get(step.name)
    started = time.monotonic()
    if handler is None:
        elapsed = time.monotonic() - started
        return RefreshStepResult(
            name=step.name,
            command=[],
            returncode=1,
            elapsed_secs=round(elapsed, 3),
            status="failed",
            output_tail=f"no inline handler registered for {step.name!r}",
        )
    try:
        ok, output = handler(config)
        rc = 0 if ok else 1
    except Exception as exc:
        ok = False
        rc = 1
        output = f"inline handler raised: {exc!r}"
    elapsed = time.monotonic() - started
    return RefreshStepResult(
        name=step.name,
        command=[],
        returncode=rc,
        elapsed_secs=round(elapsed, 3),
        status="ok" if ok else "failed",
        output_tail=_output_tail(output or ""),
    )


def _max_mtime(paths: Iterable[Path]) -> Optional[float]:
    """Return the max mtime across the given paths, or None if all missing."""
    out: Optional[float] = None
    for p in paths:
        try:
            if not p.exists():
                continue
            mt = p.stat().st_mtime
        except OSError:
            continue
        if out is None or mt > out:
            out = mt
    return out


def _max_dir_mtime(roots: Iterable[Path]) -> Optional[float]:
    """Cheaply scan ``roots`` recursively, returning the latest directory mtime.

    Used for huge corpora (e.g. ``data/games/regular/<year>/<month>/<day>``)
    where leaf-file globbing is expensive. Directory mtime updates whenever
    a file is added or removed, which is the change we care about.
    """
    out: Optional[float] = None
    for root in roots:
        if not root.exists():
            continue
        try:
            stack = [root]
            while stack:
                d = stack.pop()
                try:
                    mt = d.stat().st_mtime
                except OSError:
                    continue
                if out is None or mt > out:
                    out = mt
                try:
                    for child in d.iterdir():
                        if child.is_dir():
                            stack.append(child)
                except OSError:
                    continue
        except OSError:
            continue
    return out


def _is_step_fresh(check: StalenessCheck) -> Tuple[bool, str]:
    """Return (fresh, note). Step is fresh when output mtime >= every input mtime."""
    if not check.output_path.exists():
        return False, f"output {check.output_path.name} missing"
    try:
        output_mtime = check.output_path.stat().st_mtime
    except OSError as exc:
        return False, f"output stat failed: {exc}"

    input_mtime = _max_mtime(check.input_paths)
    dir_mtime = _max_dir_mtime(check.input_dir_mtime_roots)
    candidates = [m for m in (input_mtime, dir_mtime) if m is not None]
    if not candidates:
        return False, "no inputs reachable; running to be safe"
    newest_input = max(candidates)
    if output_mtime >= newest_input:
        delta = output_mtime - newest_input
        return True, f"output is newer than newest input by {delta:.0f}s"
    delta = newest_input - output_mtime
    return False, f"output is {delta:.0f}s older than newest input"


def _run_step(step: RefreshStep, config: RefreshConfig) -> RefreshStepResult:
    if step.kind == "inline":
        return _run_inline_step(step, config)

    # Skip-if-fresh check: only for subprocess steps with a staleness policy.
    if step.staleness_check is not None and not config.force_retrain:
        fresh, note = _is_step_fresh(step.staleness_check)
        if fresh:
            return RefreshStepResult(
                name=step.name,
                command=step.command,
                returncode=0,
                elapsed_secs=0.0,
                status="skipped_fresh",
                output_tail=f"skip: {note}",
            )

    env = os.environ.copy()
    existing_pythonpath = env.get("PYTHONPATH", "")
    paths = [str(_config.PROJECT_DIR)]
    if existing_pythonpath:
        paths.append(existing_pythonpath)
    env["PYTHONPATH"] = os.pathsep.join(paths)

    started = time.monotonic()
    proc = subprocess.run(
        step.command,
        cwd=str(_config.PROJECT_DIR),
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    elapsed = time.monotonic() - started
    status = "ok" if proc.returncode == 0 else "failed"
    return RefreshStepResult(
        name=step.name,
        command=step.command,
        returncode=proc.returncode,
        elapsed_secs=round(elapsed, 3),
        status=status,
        output_tail=_output_tail(proc.stdout or ""),
    )
