"""Stage-1 OU cache promotion guard + handler.

Promotes the staging Stage-1 cache to production after a sanity check
(game-count floor + coverage window). Refuses to promote when staging
looks like a partial scrape; never fails the refresh.
"""
from __future__ import annotations

import os
from typing import List, Optional, Tuple

from .config import (
    DEFAULT_MLB_OU_CACHE_STAGING_PATH,
    RefreshConfig,
    STAGE1_PROMOTE_MIN_GAMES_RATIO,
)
from .preflight import _inline, _safe_load_json, _stage1_cache_health


def _stage1_total_games(payload: object) -> Optional[int]:
    """Read total_games out of the Stage-1 cache meta block (canonical
    schema: payload["meta"]["total_games"], fallback "games_loaded")."""
    if not isinstance(payload, dict):
        return None
    meta = payload.get("meta")
    if not isinstance(meta, dict):
        return None
    for key in ("total_games", "games_loaded"):
        v = meta.get(key)
        if isinstance(v, (int, float)) and v > 0:
            return int(v)
    return None


def _stage1_promotion_guard(
    staging_payload: object,
    production_payload: Optional[object],
    *,
    active_date: str,
    min_games_ratio: float = STAGE1_PROMOTE_MIN_GAMES_RATIO,
) -> Tuple[bool, str]:
    """Decide whether the staging Stage-1 cache is safe to promote.

    Returns (ok_to_promote, reason). Conservative: any structural problem
    blocks promotion. The legitimate first-run case (production missing)
    is allowed as long as the staging cache passes its own coverage check.
    """
    if not isinstance(staging_payload, dict):
        return False, "staging payload is not a dict"
    staging_games = _stage1_total_games(staging_payload)
    if staging_games is None or staging_games <= 0:
        return False, "staging cache has no total_games metadata; refusing to promote"

    coverage_ok, coverage_note = _stage1_cache_health(staging_payload, active_date)
    if not coverage_ok or coverage_note.startswith("WARNING"):
        return False, f"staging coverage check rejected: {coverage_note}"

    if production_payload is None or not isinstance(production_payload, dict):
        return True, (
            f"production cache missing; promoting staging "
            f"(games={staging_games}). First-run case."
        )
    prod_games = _stage1_total_games(production_payload)
    if prod_games is None or prod_games <= 0:
        # Production exists but is malformed; staging is at least readable.
        return True, (
            f"production cache present but missing total_games; promoting staging "
            f"(games={staging_games}) to recover."
        )
    floor = int(prod_games * min_games_ratio)
    if staging_games < floor:
        return False, (
            f"staging has {staging_games} games vs production {prod_games} "
            f"(< {min_games_ratio:.0%} floor of {floor}); refusing to promote. "
            "Likely a partial scrape; investigate before retrying."
        )
    return True, (
        f"sanity guard passed: staging {staging_games} games >= "
        f"{min_games_ratio:.0%} of production {prod_games}."
    )


@_inline("stage1_cache_promote")
def _handle_stage1_cache_promote(config: RefreshConfig) -> Tuple[bool, str]:
    """Promote the staging Stage-1 cache to production after a sanity check.

    Stage-1 is a deterministic empirical lookup, not learned weights, so
    the right default is auto-promote; the guard's only job is to refuse
    when the staging cache looks broken (partial scrape, corrupt file,
    season window narrowed). Never fails the refresh; descriptive only.
    """
    notes: List[str] = []
    staging_path = DEFAULT_MLB_OU_CACHE_STAGING_PATH
    prod_path = config.mlb_ou_cache_path

    if not staging_path.exists():
        notes.append(
            f"Stage-1 staging cache missing at {staging_path.name} (rebuild step skipped?). "
            "Production cache untouched."
        )
        return True, "\n".join(notes)

    staging_payload, staging_err = _safe_load_json(staging_path)
    if staging_err:
        notes.append(
            f"ALERT Stage-1 staging cache unreadable ({staging_err}); refusing to promote."
        )
        return True, "\n".join(notes)
    prod_payload, _ = _safe_load_json(prod_path)
    promote_ok, reason = _stage1_promotion_guard(
        staging_payload, prod_payload, active_date=config.active_date
    )
    if not promote_ok:
        notes.append(
            f"ALERT Stage-1 promotion BLOCKED: {reason} "
            f"Production cache at {prod_path.name} kept; "
            f"inspect {staging_path.name} before next refresh."
        )
        return True, "\n".join(notes)
    # Promote: atomic on-disk swap (write-temp + replace) so a crash
    # mid-promotion can't leave a half-written production file.
    try:
        prod_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = prod_path.with_suffix(prod_path.suffix + ".promote_tmp")
        # On Windows os.replace handles cross-file atomic move.
        tmp_path.write_bytes(staging_path.read_bytes())
        os.replace(tmp_path, prod_path)
    except OSError as exc:
        notes.append(
            f"ALERT Stage-1 promotion FAILED during file swap: {exc!r}. "
            f"Production cache at {prod_path.name} may be in inconsistent state."
        )
        return True, "\n".join(notes)
    notes.append(
        f"ok Stage-1 promoted: {staging_path.name} -> {prod_path.name}. {reason}"
    )
    return True, "\n".join(notes)
