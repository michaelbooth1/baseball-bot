#!/usr/bin/env python3
"""Unified promotion CLI for the four manual self-improvement levers.

The daily refresh's stability gates compute "PROMOTION READY" verdicts
on Stage-2 (Brier diff stability), Stage-3 v2 (research-vs-prod beta
drift), stake-scaling (high-vs-low cohort) and per-gate threshold
(walk-forward certification's KEEP/RETUNE/RETIRE). Until this CLI
shipped, each lever had its own promote-command pattern, its own
staging path, its own implicit "did the operator inspect the right
thing first" checklist.

This file wraps all four behind one command pattern:

    python scripts/analysis/promote.py status
    python scripts/analysis/promote.py stage2          [--dry-run] [--force]
    python scripts/analysis/promote.py stage3-v2       [--dry-run] [--force]
    python scripts/analysis/promote.py stake-scaling   [--dry-run] [--force]
    python scripts/analysis/promote.py gate-threshold <gate> <value> [--dry-run] [--force]

Each subcommand:
  1. Reads the relevant verdict file built by `run_daily_refresh.py`.
  2. Refuses to promote unless the verdict says go (or `--force`).
  3. Performs the swap (file copy / subprocess / printed CLI hint).
  4. Writes a row to `data/analysis_output/promotion_events.jsonl`.
  5. Prints a next-action checklist for the operator.

Stage-2 and Stage-3 v2 actually swap files (the live runtime reads
production weights from disk). Stake-scaling and gate-threshold print
the recommended live-engine CLI flag change instead of mutating runtime
state -- the operator's saved/memorized command line stays the single
source of truth, and the promote CLI hands them the new flag value.
v2 will introduce a runtime-overrides config layer so even those
subcommands can mutate state directly; for now the print-and-log shape
keeps the operator's mental model intact.

Closes the gap between "system says go" and "system actually goes"
without removing human consent.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional


PROJECT_DIR = Path(__file__).resolve().parents[2]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from scripts.analysis.run_daily_refresh import (  # noqa: E402
    DEFAULT_STAGE2_BRIER_HISTORY_PATH,
    DEFAULT_STAGE2_CACHE_PATH,
    DEFAULT_STAGE3_V2_DRIFT_HISTORY_PATH,
    DEFAULT_STAGE3_V2_PROD_WEIGHTS_PATH,
    DEFAULT_STAGE3_V2_RESEARCH_FIT_PATH,
    _extract_stage3_v2_active_betas,
    _extract_stage3_v2_research_betas,
    _load_stage2_brier_history,
    _load_stage3_v2_drift_history,
    _safe_load_json,
    _stage2_promotion_verdict,
    _stage2_validation_brier,
    _stage3_v2_max_abs_delta,
    _stage3_v2_promotion_verdict,
)

# Runtime overrides layer (shipped 2026-05-16). Lets stake-scaling and
# gate-threshold subcommands mutate `cache/live_engine_overrides.json`
# the same way Stage-2/Stage-3-v2 mutate their cache files, so the
# auto-daemon can actuate all four levers (not just two).
from scripts.trading.live_engine_overrides import (  # noqa: E402
    DEFAULT_OVERRIDES_PATH as DEFAULT_LIVE_ENGINE_OVERRIDES_PATH,
    backup_path_for as _live_overrides_backup_path,
    remove_override as _live_overrides_remove,
    restore_overrides_from_backup as _live_overrides_restore,
    set_override as _live_overrides_set,
)


DEFAULT_STAGE2_STAGING_PATH = PROJECT_DIR / "cache" / "mlb_stage2_run_env.staging.json"
DEFAULT_PROMOTION_EVENTS_LOG = (
    PROJECT_DIR / "data" / "analysis_output" / "promotion_events.jsonl"
)
DEFAULT_STAKE_SCALING_REPORT_PATH = (
    PROJECT_DIR
    / "data"
    / "analysis_output"
    / "stake_scaling_analysis"
    / "stake_scaling_analysis.json"
)
DEFAULT_WALK_FORWARD_CERT_PATH = (
    PROJECT_DIR
    / "data"
    / "analysis_output"
    / "walk_forward_certification"
    / "walk_forward_certification.json"
)
DEFAULT_PROMOTE_TEAM_OFFENSE_SCRIPT = (
    PROJECT_DIR / "scripts" / "analysis" / "promote_team_offense_v2.py"
)


# ---------------------------------------------------------------------------
# Promotion event log
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _resolve_operator(arg_value: Optional[str]) -> str:
    if arg_value:
        return str(arg_value)
    env = os.environ.get("USER") or os.environ.get("USERNAME") or ""
    return str(env) or "unknown"


@dataclass
class PromotionEvent:
    lever: str            # "stage2" | "stage3_v2" | "stake_scaling" | "gate_threshold"
    # action: short label. Interpretation depends on `direction`:
    #   direction=promote: "promoted" | "dry_run" | "blocked" | "forced"
    #   direction=demote:  "demoted"  | "dry_run" | "blocked" | "forced"
    # ("forced" means --force was used to override a no-go verdict.)
    action: str
    operator: str
    direction: str = "promote"  # "promote" | "demote"
    # Side this event affects. Phase B B2 (2026-05-16). "both" is the
    # default for side-symmetric levers (stage2 + stage3-v2 weights
    # affect both Over and Under inference equally because the FV is
    # over-side and Under is derived as 1-FV through the per-side
    # calibrator). Side-asymmetric levers (stake-scaling, gate-threshold,
    # future per-side calibrator promotions) record "over" or "under".
    # Legacy rows without `side` are read as "both" for back-compat.
    side: str = "both"     # "over" | "under" | "both"
    verdict_snapshot: Optional[Dict[str, Any]] = None
    from_state: Optional[Dict[str, Any]] = None
    to_state: Optional[Dict[str, Any]] = None
    block_reason: Optional[str] = None
    subprocess_returncode: Optional[int] = None
    notes: Optional[str] = None
    # Path to the prior-state backup file written at promotion time, so
    # demotion can locate the rollback target without guessing. None for
    # CLI-flag levers (stake-scaling, gate-threshold) and for first-time
    # promotions where no prior production file existed.
    backup_path: Optional[str] = None
    # Active #16 (2026-05-17): lineage tracking. `source_artifact_lineage`
    # is the BUILD-time lineage pulled from the artifact JSON being
    # promoted (which builder produced it, from which git_sha, with
    # which input hashes). `promotion_lineage` is the lineage stamped
    # AT PROMOTION (current git_sha + timestamp). Together they let
    # operators trace: "fast_demote fired -> which artifact was in
    # production? -> what built it? -> who promoted it from where?"
    # Both default to None for back-compat reads.
    source_artifact_lineage: Optional[Dict[str, Any]] = None
    promotion_lineage: Optional[Dict[str, Any]] = None

    def to_row(self) -> Dict[str, Any]:
        row: Dict[str, Any] = {
            "generated_at_utc": _now_iso(),
            "lever": self.lever,
            "action": self.action,
            "direction": self.direction,
            "side": self.side,
            "operator": self.operator,
        }
        if self.verdict_snapshot is not None:
            row["verdict_snapshot"] = self.verdict_snapshot
        if self.from_state is not None:
            row["from_state"] = self.from_state
        if self.to_state is not None:
            row["to_state"] = self.to_state
        if self.block_reason:
            row["block_reason"] = self.block_reason
        if self.subprocess_returncode is not None:
            row["subprocess_returncode"] = self.subprocess_returncode
        if self.notes:
            row["notes"] = self.notes
        if self.backup_path:
            row["backup_path"] = self.backup_path
        if self.source_artifact_lineage:
            row["source_artifact_lineage"] = self.source_artifact_lineage
        if self.promotion_lineage:
            row["promotion_lineage"] = self.promotion_lineage
        return row


def write_promotion_event(
    event: PromotionEvent, *, log_path: Path = DEFAULT_PROMOTION_EVENTS_LOG
) -> None:
    """Append one event row. Best-effort: a failed write logs to stderr
    but doesn't abort the promotion (the side effect we care about
    already happened on disk)."""
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(event.to_row()) + "\n")
    except OSError as exc:
        print(
            f"WARNING failed to append promotion event to {log_path}: {exc!r}",
            file=sys.stderr,
        )


# ---------------------------------------------------------------------------
# Common output helpers
# ---------------------------------------------------------------------------


def _print_header(title: str) -> None:
    print("=" * 72)
    print(title)
    print("=" * 72)


def _print_block(label: str, value: Any) -> None:
    print(f"  {label}: {value}")


def _print_checklist(items: List[str]) -> None:
    print()
    print("Next actions:")
    for item in items:
        print(f"  - {item}")


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
    return _pl(project_root=PROJECT_DIR)


def _backup_path(prod_path: Path) -> Path:
    """The conventional location for the pre-promotion backup of a
    production file. Symmetric across all file-swap levers so the
    demotion code knows where to look."""
    return prod_path.with_suffix(prod_path.suffix + ".prior_promote.json")


def _backup_prior_production(prod_path: Path) -> Optional[Path]:
    """Atomically copy the current production file to its backup
    location BEFORE a swap. Returns the backup path on success, None
    when the production file doesn't exist (first-promotion case --
    demotion will revert by deleting the new production file).

    Atomic-write pattern matches `_atomic_copy` so a crash mid-backup
    can't leave a partial backup that demotion would then trust.
    """
    if not prod_path.exists():
        return None
    backup = _backup_path(prod_path)
    backup.parent.mkdir(parents=True, exist_ok=True)
    tmp = backup.with_suffix(backup.suffix + ".backup_tmp")
    shutil.copy2(prod_path, tmp)
    os.replace(tmp, backup)
    return backup


# ---------------------------------------------------------------------------
# Demotion infrastructure: outcome-based "did the promotion actually help?"
#
# Each lever's demotion verdict reads the most recent promotion event for
# that lever, computes filled-bet outcomes for the K days BEFORE and AFTER
# the promotion timestamp, and fires `demote` when post is materially
# worse than pre. Symmetric to the promotion stability gate, but a single
# post-hoc test rather than a multi-day stability sequence.
#
# Constants picked for ~3.4 fills/day baseline rate: 14d windows give
# ~48 expected fills per side; min_filled=10 catches even sparse weeks.
# ROI regression threshold of 10pp is large enough that small-sample noise
# doesn't false-trigger but small enough to catch a truly bad promotion.
# ---------------------------------------------------------------------------


DEMOTE_PRE_POST_WINDOW_DAYS = 14

# Active #13 (2026-05-17): fast Wilson-UB demote check thresholds.
# The slow check above needs 14d pre+post windows at our ~3.4
# fills/day rate -- a clearly-broken promotion runs for ~4 weeks
# before demoting. The fast check fires when N >= 20 post-
# promotion bets show a Wilson upper bound on win rate BELOW the
# average entry_ask (the implied probability we paid for). At 95%
# one-sided confidence this means "even the most generous
# estimate of true win rate puts us below breakeven."
#
# Phase C v2 motivation: a bad two-sided MM promotion leaks
# inventory both ways and bleeds spread to the wrong
# counterparties. With the 14d check that's ~$400-600 of
# downside. With the fast check (~5-6 days at our rate) it's
# ~$150-200. This is the safety improvement that makes Phase C v2
# actuatable.
FAST_DEMOTE_MIN_POST_FILLS = 20
FAST_DEMOTE_Z = 1.645              # one-sided 95% confidence
FAST_DEMOTE_GRACE_DAYS = 1         # don't fire same-day as promotion
DEMOTE_MIN_FILLED_PER_WINDOW = 10
DEMOTE_ROI_REGRESSION_THRESHOLD = -0.10
DEFAULT_SESSIONS_DIR = PROJECT_DIR / "data" / "live_trading" / "sessions"
# Window for "promotion happened recently" attribution on drift alerts.
# Same shape as the demote post-window but used downstream by the
# daily-review block to add "; coincides with stage2 promotion N days ago"
# to alert text.
PROMOTION_ATTRIBUTION_WINDOW_DAYS = 14


def load_promotion_events(
    log_path: Path = DEFAULT_PROMOTION_EVENTS_LOG,
) -> List[Dict[str, Any]]:
    """Read the append-only promotion events log. Missing/malformed
    lines are skipped silently -- this is research data, not a
    contract; corruption shouldn't break the demote CLI."""
    rows: List[Dict[str, Any]] = []
    if not log_path.exists():
        return rows
    try:
        with open(log_path, "r", encoding="utf-8") as f:
            for raw in f:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    rows.append(json.loads(raw))
                except json.JSONDecodeError:
                    continue
    except OSError:
        return []
    return rows


def latest_promotion_event_for_lever(
    events: List[Dict[str, Any]], lever: str,
    *, side: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Most recent successful promotion event for `lever`. Filters to
    direction=promote (or absent, which legacy-treats as promote) and
    action in {promoted, forced}. Returns None if no such event exists.

    Phase B B2 (2026-05-16) added the optional `side` filter:
      - None (default): no side filter (back-compat behavior)
      - "over"/"under": match rows whose side equals that value OR
        rows whose side is "both" (since "both"-side promotions
        affect every requested side)
      - "both": match only rows whose side is exactly "both"
        (used by callers that want to find ONLY side-symmetric
        events like a Stage-2 promotion)
    Legacy rows without `side` are treated as "both" so they always
    match an "over"/"under" filter.
    """
    candidates: List[Dict[str, Any]] = []
    for r in events:
        if str(r.get("lever") or "") != lever:
            continue
        # Backward-compat: rows without `direction` predate the field
        # and were all promotions.
        direction = str(r.get("direction") or "promote")
        if direction != "promote":
            continue
        if str(r.get("action") or "") not in ("promoted", "forced"):
            continue
        if side is not None:
            row_side = str(r.get("side") or "both")
            if side == "both":
                if row_side != "both":
                    continue
            else:
                # Side-specific filter ("over" or "under"): match same
                # side OR "both" (legacy rows + side-symmetric levers).
                if row_side != side and row_side != "both":
                    continue
        candidates.append(r)
    if not candidates:
        return None
    return max(candidates, key=lambda r: str(r.get("generated_at_utc") or ""))


def _parse_iso_date(ts: str) -> Optional[datetime]:
    """Parse the YYYY-MM-DD prefix of an ISO timestamp string."""
    try:
        return datetime.strptime(str(ts)[:10], "%Y-%m-%d")
    except (TypeError, ValueError):
        return None


def _shift_date(date_str: str, days: int) -> str:
    return (datetime.strptime(date_str, "%Y-%m-%d") + timedelta(days=days)).strftime("%Y-%m-%d")


def _list_session_dates_in_window(
    sessions_dir: Path, start_date: str, end_date: str
) -> List[str]:
    """Return YYYY-MM-DD dates of session files whose names fall in
    [start_date, end_date]. Inclusive on both ends."""
    if not sessions_dir.exists():
        return []
    out: List[str] = []
    import re as _re
    pat = _re.compile(r"^(\d{4}-\d{2}-\d{2})_session\.json$")
    for child in sessions_dir.iterdir():
        m = pat.match(child.name)
        if not m:
            continue
        d = m.group(1)
        if start_date <= d <= end_date:
            out.append(d)
    return sorted(out)


def _filled_bets_in_window(
    sessions_dir: Path,
    start_date: str,
    end_date: str,
    bet_filter: Optional[Any] = None,
) -> List[Dict[str, Any]]:
    """Load filled bets across session files in [start_date, end_date].

    Excludes paper-fallback bets (we want REAL-money outcomes for the
    demotion verdict; paper fallbacks have no real P&L attached). If
    `bet_filter(bet) -> bool` is supplied, only bets passing the filter
    are kept (used by stake-scaling to filter to multiplier-affected bets).
    """
    out: List[Dict[str, Any]] = []
    for date in _list_session_dates_in_window(sessions_dir, start_date, end_date):
        path = sessions_dir / f"{date}_session.json"
        try:
            session = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        for bet in session.get("bets") or []:
            if str(bet.get("placement_mode") or "live") != "live":
                continue
            if str(bet.get("order_status") or "") != "filled":
                continue
            if bet_filter is not None and not bet_filter(bet):
                continue
            out.append(bet)
    return out


def _summarize_filled_bets(bets: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Aggregate ROI / WR / counts. Used for both pre and post windows."""
    n = len(bets)
    wins = sum(1 for b in bets if (float(b.get("profit") or 0)) > 0)
    losses = sum(1 for b in bets if (float(b.get("profit") or 0)) < 0)
    profit = sum(float(b.get("profit") or 0) for b in bets)
    stake = sum(float(b.get("fill_cost") or b.get("stake") or 0) for b in bets)
    roi = (profit / stake) if stake else None
    wr = (wins / n) if n else None
    return {
        "n_filled": n,
        "wins": wins,
        "losses": losses,
        "total_profit": round(profit, 2),
        "total_stake": round(stake, 2),
        "roi": round(roi, 4) if roi is not None else None,
        "wr": round(wr, 4) if wr is not None else None,
    }


def _demotion_verdict_from_summaries(
    pre_summary: Dict[str, Any],
    post_summary: Dict[str, Any],
    *,
    min_filled: int = DEMOTE_MIN_FILLED_PER_WINDOW,
    regression_threshold: float = DEMOTE_ROI_REGRESSION_THRESHOLD,
) -> Dict[str, Any]:
    """Common verdict logic: enough data on both sides + post-pre ROI
    regression past threshold => demote, else hold."""
    base: Dict[str, Any] = {
        "pre_window": pre_summary,
        "post_window": post_summary,
        "min_filled_per_window": min_filled,
        "regression_threshold": regression_threshold,
    }
    if pre_summary["n_filled"] < min_filled:
        return {**base, "verdict": "insufficient_pre_data"}
    if post_summary["n_filled"] < min_filled:
        return {**base, "verdict": "insufficient_post_data"}
    pre_roi = pre_summary["roi"]
    post_roi = post_summary["roi"]
    if pre_roi is None or post_roi is None:
        return {**base, "verdict": "insufficient_pre_data"}
    delta = post_roi - pre_roi
    base["roi_delta"] = round(delta, 4)
    if delta <= regression_threshold:
        return {**base, "verdict": "demote"}
    return {**base, "verdict": "hold"}


def _wilson_upper_bound(*, wins: int, n: int, z: float) -> float:
    """One-sided Wilson upper bound on the success rate of n trials
    with `wins` successes. Used by the fast demote check.

    The Wilson interval is more accurate than the normal approximation
    at small N (which is the regime we operate in: ~20 fills/week per
    lever). The formula:

        p_hat = wins / n
        denom = 1 + z^2/n
        center = (p_hat + z^2/(2n)) / denom
        half_width = z/denom * sqrt(p_hat*(1-p_hat)/n + z^2/(4n^2))
        ub = center + half_width

    Returns 1.0 when n <= 0 (no evidence; assume the best case for
    the policy so we don't demote on zero-data).
    """
    if n <= 0:
        return 1.0
    p_hat = wins / n
    denom = 1.0 + (z * z) / n
    center = (p_hat + (z * z) / (2.0 * n)) / denom
    inside = p_hat * (1.0 - p_hat) / n + (z * z) / (4.0 * n * n)
    if inside < 0:
        inside = 0.0
    half_width = (z / denom) * math.sqrt(inside)
    ub = center + half_width
    # Clamp to [0, 1]; the Wilson formula stays inside the unit
    # interval for n >= 1, but floating-point round-off can push by
    # 1e-15 at the boundaries.
    return min(1.0, max(0.0, ub))


def _fast_wilson_demote_from_post_bets(
    post_bets: List[Dict[str, Any]],
    *,
    min_post_fills: int = FAST_DEMOTE_MIN_POST_FILLS,
    z: float = FAST_DEMOTE_Z,
) -> Dict[str, Any]:
    """Compute the fast Wilson-UB demote verdict from one window of
    post-promotion filled bets.

    Verdict taxonomy:
      - `fast_demote`: N >= min_post_fills AND Wilson UB on win rate
        is below the average entry_ask (breakeven). Statistically
        confident the policy is losing money.
      - `hold`: N >= min_post_fills but Wilson UB still >= breakeven.
        No evidence of failure yet.
      - `insufficient_post_data`: N < min_post_fills. UB too wide to
        be useful.

    The breakeven rate uses MEAN entry_ask across the post-window
    bets. At a 0.70 average ask, payout per win = 1/0.70 - 1 = 0.43;
    you must win >= 70% of the time to break even on that mix. If
    Wilson UB on win rate is < 0.70, even the most generous estimate
    of the true win rate falls below breakeven.
    """
    n = len(post_bets)
    wins = sum(
        1 for b in post_bets if (float(b.get("profit") or 0)) > 0
    )
    asks = [
        float(b["entry_ask"])
        for b in post_bets
        if b.get("entry_ask") is not None
    ]
    mean_ask = (sum(asks) / len(asks)) if asks else None

    base: Dict[str, Any] = {
        "n_post_filled": n,
        "wins_post": wins,
        "observed_win_rate": round(wins / n, 4) if n else None,
        "mean_entry_ask": (
            round(mean_ask, 4) if mean_ask is not None else None
        ),
        "wilson_ub_win_rate": None,
        "min_post_fills": min_post_fills,
        "z": z,
    }

    if n < min_post_fills:
        return {**base, "verdict": "insufficient_post_data"}
    if mean_ask is None:
        # No entry_ask -> can't compute breakeven. Treat as
        # insufficient_post_data rather than firing demote.
        return {**base, "verdict": "insufficient_post_data"}

    ub = _wilson_upper_bound(wins=wins, n=n, z=z)
    base["wilson_ub_win_rate"] = round(ub, 4)
    base["breakeven_win_rate"] = round(mean_ask, 4)
    base["wilson_ub_vs_breakeven_delta"] = round(ub - mean_ask, 4)

    if ub < mean_ask:
        return {**base, "verdict": "fast_demote"}
    return {**base, "verdict": "hold"}


def _per_lever_fast_demote_verdict(
    *,
    lever: str,
    promotion_event: Optional[Dict[str, Any]],
    sessions_dir: Path,
    bet_filter: Optional[Any] = None,
    min_post_fills: int = FAST_DEMOTE_MIN_POST_FILLS,
    z: float = FAST_DEMOTE_Z,
    grace_days: int = FAST_DEMOTE_GRACE_DAYS,
    today: Optional[str] = None,
) -> Dict[str, Any]:
    """Compute the fast Wilson-UB demote verdict for one lever.

    Reads the most recent promotion event's post-promotion bet
    window (promotion_date + 1 -> today) and runs the Wilson check.
    `grace_days` enforces a minimum gap between promotion timestamp
    and the earliest day we count post-window bets (default 1 -- so
    same-day bets after a morning promotion don't dominate the
    sample with intraday noise).
    """
    if promotion_event is None:
        return {
            "verdict": "no_promotion_to_demote",
            "lever": lever,
            "promotion_event": None,
        }
    pdate_dt = _parse_iso_date(promotion_event.get("generated_at_utc") or "")
    if pdate_dt is None:
        return {
            "verdict": "no_promotion_to_demote",
            "lever": lever,
            "promotion_event": promotion_event,
            "block_reason": "promotion event has unparseable timestamp",
        }
    pdate = pdate_dt.strftime("%Y-%m-%d")
    post_start = _shift_date(pdate, grace_days)
    today_str = today or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if today_str < post_start:
        # Within grace period -- not enough time has elapsed since
        # promotion to start counting.
        return {
            "verdict": "within_grace_period",
            "lever": lever,
            "promotion_event": {
                "generated_at_utc": promotion_event.get("generated_at_utc"),
                "operator": promotion_event.get("operator"),
                "action": promotion_event.get("action"),
            },
            "post_window_dates": {"start": post_start, "end": today_str},
            "grace_days": grace_days,
        }
    post_bets = _filled_bets_in_window(
        sessions_dir, post_start, today_str, bet_filter=bet_filter,
    )
    verdict = _fast_wilson_demote_from_post_bets(
        post_bets, min_post_fills=min_post_fills, z=z,
    )
    verdict["lever"] = lever
    verdict["promotion_event"] = {
        "generated_at_utc": promotion_event.get("generated_at_utc"),
        "operator": promotion_event.get("operator"),
        "action": promotion_event.get("action"),
        "from_state": promotion_event.get("from_state"),
        "to_state": promotion_event.get("to_state"),
        "backup_path": promotion_event.get("backup_path"),
    }
    verdict["post_window_dates"] = {"start": post_start, "end": today_str}
    verdict["grace_days"] = grace_days
    return verdict


def _per_lever_demotion_verdict(
    *,
    lever: str,
    promotion_event: Optional[Dict[str, Any]],
    sessions_dir: Path,
    bet_filter: Optional[Any] = None,
    window_days: int = DEMOTE_PRE_POST_WINDOW_DAYS,
    min_filled: int = DEMOTE_MIN_FILLED_PER_WINDOW,
    regression_threshold: float = DEMOTE_ROI_REGRESSION_THRESHOLD,
) -> Dict[str, Any]:
    """Compute a demotion verdict for one lever from its most recent
    promotion event + session bet outcomes."""
    if promotion_event is None:
        return {
            "verdict": "no_promotion_to_demote",
            "lever": lever,
            "promotion_event": None,
        }
    pdate_dt = _parse_iso_date(promotion_event.get("generated_at_utc") or "")
    if pdate_dt is None:
        return {
            "verdict": "no_promotion_to_demote",
            "lever": lever,
            "promotion_event": promotion_event,
            "block_reason": "promotion event has unparseable timestamp",
        }
    pdate = pdate_dt.strftime("%Y-%m-%d")
    pre_start = _shift_date(pdate, -window_days)
    pre_end = _shift_date(pdate, -1)
    post_start = pdate
    post_end = _shift_date(pdate, window_days - 1)
    pre_bets = _filled_bets_in_window(sessions_dir, pre_start, pre_end, bet_filter=bet_filter)
    post_bets = _filled_bets_in_window(sessions_dir, post_start, post_end, bet_filter=bet_filter)
    verdict = _demotion_verdict_from_summaries(
        _summarize_filled_bets(pre_bets),
        _summarize_filled_bets(post_bets),
        min_filled=min_filled,
        regression_threshold=regression_threshold,
    )
    verdict["lever"] = lever
    verdict["promotion_event"] = {
        # Compact subset; full event is already in the audit log.
        "generated_at_utc": promotion_event.get("generated_at_utc"),
        "operator": promotion_event.get("operator"),
        "action": promotion_event.get("action"),
        "from_state": promotion_event.get("from_state"),
        "to_state": promotion_event.get("to_state"),
        "backup_path": promotion_event.get("backup_path"),
    }
    verdict["pre_window_dates"] = {"start": pre_start, "end": pre_end}
    verdict["post_window_dates"] = {"start": post_start, "end": post_end}
    return verdict


# Per-lever bet filters. stage2/stage3-v2 affect every prediction so no
# filter; stake-scaling only affects bets where the multiplier deviated
# from 1.0; gate-threshold uses overall ROI as a proxy (correctly attributing
# "would have been blocked by old threshold" is messy and at our sample
# sizes the noise from the overall comparison is similar).
def _stake_scaling_bet_filter(bet: Dict[str, Any]) -> bool:
    m = bet.get("calibrated_stake_multiplier")
    if m is None:
        return False
    try:
        return abs(float(m) - 1.0) > 1e-6
    except (TypeError, ValueError):
        return False


def stage2_demotion_verdict(
    *, events: List[Dict[str, Any]], sessions_dir: Path,
    window_days: int = DEMOTE_PRE_POST_WINDOW_DAYS,
    min_filled: int = DEMOTE_MIN_FILLED_PER_WINDOW,
    regression_threshold: float = DEMOTE_ROI_REGRESSION_THRESHOLD,
) -> Dict[str, Any]:
    return _per_lever_demotion_verdict(
        lever="stage2",
        promotion_event=latest_promotion_event_for_lever(events, "stage2"),
        sessions_dir=sessions_dir,
        bet_filter=None,
        window_days=window_days,
        min_filled=min_filled,
        regression_threshold=regression_threshold,
    )


def stage3_v2_demotion_verdict(
    *, events: List[Dict[str, Any]], sessions_dir: Path,
    window_days: int = DEMOTE_PRE_POST_WINDOW_DAYS,
    min_filled: int = DEMOTE_MIN_FILLED_PER_WINDOW,
    regression_threshold: float = DEMOTE_ROI_REGRESSION_THRESHOLD,
) -> Dict[str, Any]:
    return _per_lever_demotion_verdict(
        lever="stage3_v2",
        promotion_event=latest_promotion_event_for_lever(events, "stage3_v2"),
        sessions_dir=sessions_dir,
        bet_filter=None,
        window_days=window_days,
        min_filled=min_filled,
        regression_threshold=regression_threshold,
    )


def stake_scaling_demotion_verdict(
    *, events: List[Dict[str, Any]], sessions_dir: Path,
    window_days: int = DEMOTE_PRE_POST_WINDOW_DAYS,
    min_filled: int = DEMOTE_MIN_FILLED_PER_WINDOW,
    regression_threshold: float = DEMOTE_ROI_REGRESSION_THRESHOLD,
) -> Dict[str, Any]:
    return _per_lever_demotion_verdict(
        lever="stake_scaling",
        promotion_event=latest_promotion_event_for_lever(events, "stake_scaling"),
        sessions_dir=sessions_dir,
        bet_filter=_stake_scaling_bet_filter,
        window_days=window_days,
        min_filled=min_filled,
        regression_threshold=regression_threshold,
    )


def gate_threshold_demotion_verdict(
    *, events: List[Dict[str, Any]], sessions_dir: Path,
    window_days: int = DEMOTE_PRE_POST_WINDOW_DAYS,
    min_filled: int = DEMOTE_MIN_FILLED_PER_WINDOW,
    regression_threshold: float = DEMOTE_ROI_REGRESSION_THRESHOLD,
) -> Dict[str, Any]:
    # gate-threshold demote-verdict uses overall ROI as a proxy. A more
    # precise version would filter to bets allowed by the new threshold
    # but blocked by the old, but at our sample sizes (~3.4 fills/day)
    # filtering further makes the verdict undecidable. Operator can use
    # the broader signal, then inspect cohort-ROI alerts for finer detail.
    return _per_lever_demotion_verdict(
        lever="gate_threshold",
        promotion_event=latest_promotion_event_for_lever(events, "gate_threshold"),
        sessions_dir=sessions_dir,
        bet_filter=None,
        window_days=window_days,
        min_filled=min_filled,
        regression_threshold=regression_threshold,
    )


# ---------------------------------------------------------------------------
# Fast Wilson-UB demote verdicts (Active #13, 2026-05-17). Parallel to the
# windowed verdicts above. Either one firing can drive a demotion; the
# fast verdict typically fires sooner (5-6 days vs 14+ days) when the
# evidence is statistically clear.
# ---------------------------------------------------------------------------


def stage2_fast_demote_verdict(
    *, events: List[Dict[str, Any]], sessions_dir: Path,
    min_post_fills: int = FAST_DEMOTE_MIN_POST_FILLS,
    z: float = FAST_DEMOTE_Z,
    grace_days: int = FAST_DEMOTE_GRACE_DAYS,
    today: Optional[str] = None,
) -> Dict[str, Any]:
    return _per_lever_fast_demote_verdict(
        lever="stage2",
        promotion_event=latest_promotion_event_for_lever(events, "stage2"),
        sessions_dir=sessions_dir, bet_filter=None,
        min_post_fills=min_post_fills, z=z, grace_days=grace_days, today=today,
    )


def stage3_v2_fast_demote_verdict(
    *, events: List[Dict[str, Any]], sessions_dir: Path,
    min_post_fills: int = FAST_DEMOTE_MIN_POST_FILLS,
    z: float = FAST_DEMOTE_Z,
    grace_days: int = FAST_DEMOTE_GRACE_DAYS,
    today: Optional[str] = None,
) -> Dict[str, Any]:
    return _per_lever_fast_demote_verdict(
        lever="stage3_v2",
        promotion_event=latest_promotion_event_for_lever(events, "stage3_v2"),
        sessions_dir=sessions_dir, bet_filter=None,
        min_post_fills=min_post_fills, z=z, grace_days=grace_days, today=today,
    )


def stake_scaling_fast_demote_verdict(
    *, events: List[Dict[str, Any]], sessions_dir: Path,
    min_post_fills: int = FAST_DEMOTE_MIN_POST_FILLS,
    z: float = FAST_DEMOTE_Z,
    grace_days: int = FAST_DEMOTE_GRACE_DAYS,
    today: Optional[str] = None,
) -> Dict[str, Any]:
    return _per_lever_fast_demote_verdict(
        lever="stake_scaling",
        promotion_event=latest_promotion_event_for_lever(events, "stake_scaling"),
        sessions_dir=sessions_dir, bet_filter=_stake_scaling_bet_filter,
        min_post_fills=min_post_fills, z=z, grace_days=grace_days, today=today,
    )


def gate_threshold_fast_demote_verdict(
    *, events: List[Dict[str, Any]], sessions_dir: Path,
    min_post_fills: int = FAST_DEMOTE_MIN_POST_FILLS,
    z: float = FAST_DEMOTE_Z,
    grace_days: int = FAST_DEMOTE_GRACE_DAYS,
    today: Optional[str] = None,
) -> Dict[str, Any]:
    return _per_lever_fast_demote_verdict(
        lever="gate_threshold",
        promotion_event=latest_promotion_event_for_lever(events, "gate_threshold"),
        sessions_dir=sessions_dir, bet_filter=None,
        min_post_fills=min_post_fills, z=z, grace_days=grace_days, today=today,
    )


# ---------------------------------------------------------------------------
# `status` subcommand: read all four verdicts and print one summary.
# ---------------------------------------------------------------------------


def cmd_status(args: argparse.Namespace) -> int:
    _print_header("Promotion verdict status")

    # Stage-2
    s2_history = _load_stage2_brier_history(args.stage2_brier_history_path)
    s2_verdict = _stage2_promotion_verdict(s2_history)
    print()
    print(f"[stage2] verdict: {s2_verdict['verdict']}")
    _print_block("history", f"{s2_verdict['n_history']} distinct prior dates")
    _print_block("improving days", f"{s2_verdict['n_improving']}/{s2_verdict['n_history']} (need {s2_verdict['n_consecutive_required']})")

    # Stage-3 v2
    s3_history = _load_stage3_v2_drift_history(args.stage3_v2_drift_history_path)
    s3_verdict = _stage3_v2_promotion_verdict(s3_history)
    print()
    print(f"[stage3-v2] verdict: {s3_verdict['verdict']}")
    _print_block("history", f"{s3_verdict['n_history']} distinct prior dates")
    _print_block("drifting days", f"{s3_verdict['n_drifting']}/{s3_verdict['n_history']} (need {s3_verdict['n_consecutive_required']})")

    # Stake-scaling
    ss_payload, ss_err = _safe_load_json(args.stake_scaling_report_path)
    print()
    if ss_err:
        print(f"[stake-scaling] verdict: <unreadable: {ss_err}>")
    elif not isinstance(ss_payload, dict):
        print("[stake-scaling] verdict: <missing>")
    else:
        v = str(ss_payload.get("verdict") or "<missing>")
        print(f"[stake-scaling] verdict: {v}")
        _print_block("reason", ss_payload.get("verdict_reason", ""))
        _print_block(
            "sessions",
            f"{ss_payload.get('n_sessions', 0)}/{(ss_payload.get('thresholds') or {}).get('min_sessions', 30)}",
        )

    # Walk-forward certification
    wfc_payload, wfc_err = _safe_load_json(args.walk_forward_cert_path)
    print()
    if wfc_err:
        print(f"[gate-threshold] verdict: <unreadable: {wfc_err}>")
    elif not isinstance(wfc_payload, dict):
        print("[gate-threshold] verdict: <missing>")
    else:
        readiness = wfc_payload.get("readiness") or {}
        print(f"[gate-threshold] readiness: {readiness.get('label', '<unknown>')}")
        _print_block(
            "filled / dates", f"{readiness.get('n_filled', 0)} / {readiness.get('n_dates', 0)}"
        )
        gates = wfc_payload.get("gates") or []
        actionable = []
        for entry in gates:
            v = (entry.get("verdict") or {})
            label = str(v.get("verdict") or "")
            name = entry.get("name") or "?"
            if label.upper() in ("RETUNE", "RETIRE"):
                actionable.append(
                    f"{name} -> {label.upper()} (recommended_threshold={v.get('recommended_threshold')})"
                )
        if actionable:
            print("  actionable gates:")
            for a in actionable:
                print(f"    - {a}")
        else:
            print("  actionable gates: none (all KEEP)")

    # Demotion verdicts: only show when a recent promotion exists
    # (otherwise the verdict is uniformly "no_promotion_to_demote" and
    # adding noise to the status output isn't useful).
    sessions_dir = getattr(args, "sessions_dir", DEFAULT_SESSIONS_DIR)
    events = load_promotion_events(args.event_log_path)
    demotion_verdicts = {
        "stage2": stage2_demotion_verdict(events=events, sessions_dir=sessions_dir),
        "stage3-v2": stage3_v2_demotion_verdict(events=events, sessions_dir=sessions_dir),
        "stake-scaling": stake_scaling_demotion_verdict(events=events, sessions_dir=sessions_dir),
        "gate-threshold": gate_threshold_demotion_verdict(events=events, sessions_dir=sessions_dir),
    }
    # Active #13: fast Wilson-UB demote verdicts (parallel to the
    # windowed verdicts above). Either firing flags the lever for
    # demotion; the fast verdict typically fires sooner (5-6 days
    # vs 14+ days) when the evidence is statistically clear.
    fast_verdicts = {
        "stage2": stage2_fast_demote_verdict(events=events, sessions_dir=sessions_dir),
        "stage3-v2": stage3_v2_fast_demote_verdict(events=events, sessions_dir=sessions_dir),
        "stake-scaling": stake_scaling_fast_demote_verdict(events=events, sessions_dir=sessions_dir),
        "gate-threshold": gate_threshold_fast_demote_verdict(events=events, sessions_dir=sessions_dir),
    }
    actionable_demote = [
        (name, v, "windowed")
        for name, v in demotion_verdicts.items()
        if v["verdict"] == "demote"
    ] + [
        (name, v, "fast")
        for name, v in fast_verdicts.items()
        if v["verdict"] == "fast_demote"
    ]
    relevant = [
        (name, v) for name, v in demotion_verdicts.items()
        if v["verdict"] != "no_promotion_to_demote"
    ]
    print()
    print("=" * 72)
    print("Demotion verdict status (post-promotion outcome regression check)")
    print("=" * 72)
    if not relevant:
        print()
        print("  No recent promotions to evaluate (all four levers: no_promotion_to_demote).")
    else:
        # Index the audit log by lever so we can pull the full
        # promotion event (including lineage) for each lever shown.
        lever_audit = {
            "stage2": latest_promotion_event_for_lever(events, "stage2"),
            "stage3-v2": latest_promotion_event_for_lever(events, "stage3_v2"),
            "stake-scaling": latest_promotion_event_for_lever(events, "stake_scaling"),
            "gate-threshold": latest_promotion_event_for_lever(events, "gate_threshold"),
        }
        for name, v in relevant:
            print()
            print(f"[{name}] windowed demote verdict: {v['verdict']}")
            pe = v.get("promotion_event") or {}
            if pe.get("generated_at_utc"):
                _print_block("promoted at", pe["generated_at_utc"])
            # Active #16: surface lineage of the artifact that's in
            # production so fast_demote investigations have an immediate
            # answer to "which artifact + which git_sha?"
            audit_row = lever_audit.get(name) or {}
            src_lineage = audit_row.get("source_artifact_lineage") or {}
            promo_lineage = audit_row.get("promotion_lineage") or {}
            if src_lineage.get("git_sha") or src_lineage.get("builder_path"):
                dirty = " (dirty)" if src_lineage.get("git_dirty") else ""
                _print_block(
                    "artifact lineage",
                    f"built_at={src_lineage.get('built_at_utc') or '?'} "
                    f"by={src_lineage.get('builder_path') or '?'} "
                    f"git_sha={src_lineage.get('git_sha') or '?'}{dirty}",
                )
            if promo_lineage.get("git_sha"):
                _print_block(
                    "promoted from",
                    f"git_sha={promo_lineage['git_sha']} "
                    f"branch={promo_lineage.get('git_branch') or '?'}",
                )
            pre = v.get("pre_window") or {}
            post = v.get("post_window") or {}
            if pre and pre.get("n_filled"):
                _print_block(
                    "pre window",
                    f"n={pre.get('n_filled', 0)}  ROI={(pre.get('roi') or 0) * 100:+.1f}%",
                )
            if post and post.get("n_filled"):
                _print_block(
                    "post window",
                    f"n={post.get('n_filled', 0)}  ROI={(post.get('roi') or 0) * 100:+.1f}%",
                )
            if v.get("roi_delta") is not None:
                _print_block("ROI delta", f"{v['roi_delta'] * 100:+.1f}pp")
            # Surface the fast Wilson-UB verdict for the same lever.
            fast_v = fast_verdicts.get(name) or {}
            if fast_v.get("verdict") not in {None, "no_promotion_to_demote"}:
                print(f"[{name}] fast Wilson-UB verdict:  {fast_v['verdict']}")
                if fast_v.get("n_post_filled") is not None:
                    _print_block(
                        "fast post window",
                        f"n={fast_v['n_post_filled']}  "
                        f"wins={fast_v.get('wins_post', 0)}  "
                        f"WR_obs={(fast_v.get('observed_win_rate') or 0) * 100:.1f}%  "
                        f"UB={(fast_v.get('wilson_ub_win_rate') or 0) * 100:.1f}%  "
                        f"breakeven={(fast_v.get('breakeven_win_rate') or 0) * 100:.1f}%",
                    )
        if actionable_demote:
            print()
            print(f"ALERT {len(actionable_demote)} lever(s) flagged for demotion:")
            for name, _, kind in actionable_demote:
                print(f"  - [{kind}] run: python scripts/analysis/promote.py demote {name}")

    return 0


# ---------------------------------------------------------------------------
# `stage2` subcommand: atomic copy staging -> production cache.
# ---------------------------------------------------------------------------


def cmd_stage2(args: argparse.Namespace) -> int:
    _print_header("Promote Stage-2 staging -> production")
    operator = _resolve_operator(args.operator)
    history = _load_stage2_brier_history(args.stage2_brier_history_path)
    verdict = _stage2_promotion_verdict(history)
    _print_block("verdict", verdict["verdict"])
    _print_block(
        "improving days",
        f"{verdict['n_improving']}/{verdict['n_history']} (need {verdict['n_consecutive_required']})",
    )

    # Read current Brier values for the audit trail (not for decision logic --
    # the verdict above is authoritative). We snapshot prod + staging so the
    # event log says exactly what we swapped from / to.
    prod_payload, _ = _safe_load_json(args.stage2_cache_path)
    stg_payload, _ = _safe_load_json(args.stage2_staging_path)
    prod_brier = _stage2_validation_brier(prod_payload)
    stg_brier = _stage2_validation_brier(stg_payload)
    _print_block("staging brier", f"{stg_brier:.4f}" if stg_brier is not None else "<unreadable>")
    _print_block("production brier", f"{prod_brier:.4f}" if prod_brier is not None else "<unreadable>")

    if not args.stage2_staging_path.exists():
        print(
            f"\nERROR Stage-2 staging cache missing at {args.stage2_staging_path}",
            file=sys.stderr,
        )
        write_promotion_event(
            PromotionEvent(
                lever="stage2",
                action="blocked",
                operator=operator,
                verdict_snapshot=verdict,
                block_reason="staging cache missing",
            ),
            log_path=args.event_log_path,
        )
        return 2

    if verdict["verdict"] != "promote" and not args.force:
        msg = (
            f"verdict is '{verdict['verdict']}', not 'promote'; "
            "refusing to promote (use --force to override)"
        )
        print(f"\nBLOCKED {msg}")
        write_promotion_event(
            PromotionEvent(
                lever="stage2",
                action="blocked",
                operator=operator,
                verdict_snapshot=verdict,
                block_reason=msg,
            ),
            log_path=args.event_log_path,
        )
        return 1

    if args.dry_run:
        print(
            f"\nDRY-RUN would atomically copy "
            f"{args.stage2_staging_path.name} -> {args.stage2_cache_path.name} "
            f"(backing up prior production to {_backup_path(args.stage2_cache_path).name} first)"
        )
        write_promotion_event(
            PromotionEvent(
                lever="stage2",
                action="dry_run",
                direction="promote",
                operator=operator,
                verdict_snapshot=verdict,
                from_state={"production_brier": prod_brier},
                to_state={"staging_brier": stg_brier},
            ),
            log_path=args.event_log_path,
        )
        return 0

    # Back up the current production file BEFORE the swap so a future
    # `demote stage2` can restore it. None means there was no prior
    # production file -- shouldn't happen at this point but handled
    # defensively (demote would revert by deleting the new production).
    try:
        backup = _backup_prior_production(args.stage2_cache_path)
    except OSError as exc:
        print(f"\nERROR backup failed: {exc!r}", file=sys.stderr)
        write_promotion_event(
            PromotionEvent(
                lever="stage2",
                action="blocked",
                direction="promote",
                operator=operator,
                verdict_snapshot=verdict,
                block_reason=f"backup failed: {exc!r}",
            ),
            log_path=args.event_log_path,
        )
        return 3

    try:
        _atomic_copy(args.stage2_staging_path, args.stage2_cache_path)
    except OSError as exc:
        print(f"\nERROR file swap failed: {exc!r}", file=sys.stderr)
        write_promotion_event(
            PromotionEvent(
                lever="stage2",
                action="blocked",
                direction="promote",
                operator=operator,
                verdict_snapshot=verdict,
                block_reason=f"file swap failed: {exc!r}",
                backup_path=str(backup) if backup else None,
            ),
            log_path=args.event_log_path,
        )
        return 3

    print(
        f"\nPROMOTED Stage-2: {args.stage2_staging_path.name} -> {args.stage2_cache_path.name}"
        + (f"\n  prior production backed up to {backup.name}" if backup else "")
    )
    write_promotion_event(
        PromotionEvent(
            lever="stage2",
            action="forced" if (verdict["verdict"] != "promote" and args.force) else "promoted",
            direction="promote",
            operator=operator,
            verdict_snapshot=verdict,
            from_state={"production_brier": prod_brier},
            to_state={"staging_brier": stg_brier},
            backup_path=str(backup) if backup else None,
            # Active #16: source lineage from the staging artifact +
            # fresh promotion-time lineage. Lets fast_demote investigations
            # answer "which Stage-2 was promoted, from which git_sha?"
            source_artifact_lineage=_capture_artifact_lineage(args.stage2_staging_path),
            promotion_lineage=_compute_promotion_lineage(),
        ),
        log_path=args.event_log_path,
    )
    _print_checklist(
        [
            "Restart `live_engine.py` (or wait for next session) so the new Stage-2 cache loads.",
            "Verify by checking the next refresh's `model_freshness_health` "
            "block: prod and staging Brier should now be ~equal.",
            f"If outcomes regress, run `promote.py demote stage2` to restore from "
            f"{backup.name if backup else '<no backup>'}.",
            f"Audit trail: see {args.event_log_path} for this promotion's row.",
        ]
    )
    return 0


# ---------------------------------------------------------------------------
# `stage3-v2` subcommand: subprocess-invokes promote_team_offense_v2.py.
# ---------------------------------------------------------------------------


def cmd_stage3_v2(args: argparse.Namespace) -> int:
    _print_header("Promote Stage-3 v2 research fit -> production weights")
    operator = _resolve_operator(args.operator)
    history = _load_stage3_v2_drift_history(args.stage3_v2_drift_history_path)
    verdict = _stage3_v2_promotion_verdict(history)
    _print_block("verdict", verdict["verdict"])
    _print_block(
        "drifting days",
        f"{verdict['n_drifting']}/{verdict['n_history']} (need {verdict['n_consecutive_required']})",
    )

    research_payload, _ = _safe_load_json(args.stage3_v2_research_fit_path)
    research_betas = _extract_stage3_v2_research_betas(research_payload)
    prod_payload, _ = _safe_load_json(args.stage3_v2_prod_weights_path)
    active_betas, active_source = _extract_stage3_v2_active_betas(prod_payload)
    if research_betas is not None:
        max_delta = _stage3_v2_max_abs_delta(research_betas, active_betas)
        _print_block("max |delta|", f"{max_delta:.4f}")
        _print_block(
            "research betas",
            ", ".join(f"{k}={v:+.4f}" for k, v in sorted(research_betas.items())),
        )
        _print_block(
            f"active betas ({active_source})",
            ", ".join(f"{k}={v:+.4f}" for k, v in sorted(active_betas.items())),
        )
    else:
        _print_block("research betas", "<unreadable>")

    if research_betas is None:
        print(
            f"\nERROR could not extract Stage-3 v2 betas from "
            f"{args.stage3_v2_research_fit_path}",
            file=sys.stderr,
        )
        write_promotion_event(
            PromotionEvent(
                lever="stage3_v2",
                action="blocked",
                operator=operator,
                verdict_snapshot=verdict,
                block_reason="could not extract research betas",
            ),
            log_path=args.event_log_path,
        )
        return 2

    if verdict["verdict"] != "promote" and not args.force:
        msg = (
            f"verdict is '{verdict['verdict']}', not 'promote'; "
            "refusing to promote (use --force to override)"
        )
        print(f"\nBLOCKED {msg}")
        write_promotion_event(
            PromotionEvent(
                lever="stage3_v2",
                action="blocked",
                direction="promote",
                operator=operator,
                verdict_snapshot=verdict,
                block_reason=msg,
            ),
            log_path=args.event_log_path,
        )
        return 1

    if args.dry_run:
        print(
            f"\nDRY-RUN would invoke "
            f"`python {args.promote_team_offense_script} --source-artifact {args.stage3_v2_research_fit_path}`"
            f"\n  prior production (if present) would be backed up to "
            f"{_backup_path(args.stage3_v2_prod_weights_path).name} first"
        )
        write_promotion_event(
            PromotionEvent(
                lever="stage3_v2",
                action="dry_run",
                direction="promote",
                operator=operator,
                verdict_snapshot=verdict,
                from_state={"active_betas": active_betas, "active_source": active_source},
                to_state={"research_betas": research_betas},
            ),
            log_path=args.event_log_path,
        )
        return 0

    # Back up prior production weights (if any) before subprocess overwrites
    # them. None when this is the first promotion (compiled-defaults case);
    # demote stage3-v2 will then revert by deleting the new production file.
    try:
        backup = _backup_prior_production(args.stage3_v2_prod_weights_path)
    except OSError as exc:
        print(f"\nERROR backup failed: {exc!r}", file=sys.stderr)
        write_promotion_event(
            PromotionEvent(
                lever="stage3_v2",
                action="blocked",
                direction="promote",
                operator=operator,
                verdict_snapshot=verdict,
                block_reason=f"backup failed: {exc!r}",
            ),
            log_path=args.event_log_path,
        )
        return 3

    cmd = [
        sys.executable,
        str(args.promote_team_offense_script),
        "--source-artifact",
        str(args.stage3_v2_research_fit_path),
        "--output-path",
        str(args.stage3_v2_prod_weights_path),
    ]
    print(f"\nInvoking {' '.join(cmd)}")
    proc = subprocess.run(
        cmd,
        cwd=str(PROJECT_DIR),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    if proc.stdout:
        print(proc.stdout)
    if proc.returncode != 0:
        print(
            f"\nERROR promote_team_offense_v2.py exited {proc.returncode}",
            file=sys.stderr,
        )
        write_promotion_event(
            PromotionEvent(
                lever="stage3_v2",
                action="blocked",
                direction="promote",
                operator=operator,
                verdict_snapshot=verdict,
                block_reason=f"subprocess exit {proc.returncode}",
                subprocess_returncode=proc.returncode,
                backup_path=str(backup) if backup else None,
            ),
            log_path=args.event_log_path,
        )
        return proc.returncode

    print(
        f"\nPROMOTED Stage-3 v2: production weights updated"
        + (f"\n  prior production backed up to {backup.name}" if backup else "\n  (no prior production -- was using compiled defaults)")
    )
    write_promotion_event(
        PromotionEvent(
            lever="stage3_v2",
            action="forced" if (verdict["verdict"] != "promote" and args.force) else "promoted",
            direction="promote",
            operator=operator,
            verdict_snapshot=verdict,
            from_state={"active_betas": active_betas, "active_source": active_source},
            to_state={"research_betas": research_betas},
            subprocess_returncode=proc.returncode,
            backup_path=str(backup) if backup else None,
            source_artifact_lineage=_capture_artifact_lineage(args.stage3_v2_research_fit_path),
            promotion_lineage=_compute_promotion_lineage(),
        ),
        log_path=args.event_log_path,
    )
    _print_checklist(
        [
            "Restart `live_engine.py` (or wait for next session) so TeamOffenseModel reloads.",
            "Verify the startup log line: 'TeamOffenseModel loaded ... betas=(prior=... season=... mom10=...)' matches the new betas.",
            "Tomorrow's Stage-3 v2 promotion-readiness verdict should drop to 'insufficient_history' (history dedupes by date; the swap resets the drift baseline).",
            f"Audit trail: see {args.event_log_path} for this promotion's row.",
        ]
    )
    return 0


# ---------------------------------------------------------------------------
# `stake-scaling` subcommand: print recommended CLI flag change + log.
# ---------------------------------------------------------------------------


def cmd_stake_scaling(args: argparse.Namespace) -> int:
    _print_header("Promote stake-scaling shadow -> enforce")
    operator = _resolve_operator(args.operator)
    payload, err = _safe_load_json(args.stake_scaling_report_path)
    if err or not isinstance(payload, dict):
        print(
            f"\nERROR could not read stake-scaling report at "
            f"{args.stake_scaling_report_path}: {err}",
            file=sys.stderr,
        )
        write_promotion_event(
            PromotionEvent(
                lever="stake_scaling",
                side=getattr(args, "side", "over"),
                action="blocked",
                operator=operator,
                block_reason=f"verdict report unreadable: {err}",
            ),
            log_path=args.event_log_path,
        )
        return 2

    verdict_label = str(payload.get("verdict") or "<missing>")
    _print_block("verdict", verdict_label)
    _print_block("reason", payload.get("verdict_reason", ""))
    _print_block(
        "sessions",
        f"{payload.get('n_sessions', 0)}/{(payload.get('thresholds') or {}).get('min_sessions', 30)}",
    )

    if verdict_label != "promote" and not args.force:
        msg = (
            f"verdict is '{verdict_label}', not 'promote'; "
            "refusing to recommend enforce (use --force to override)"
        )
        print(f"\nBLOCKED {msg}")
        write_promotion_event(
            PromotionEvent(
                lever="stake_scaling",
                side=getattr(args, "side", "over"),
                action="blocked",
                operator=operator,
                verdict_snapshot=payload,
                block_reason=msg,
            ),
            log_path=args.event_log_path,
        )
        return 1

    cli_change = "--calibrated-stake-scale-mode enforce"
    print(f"\nRECOMMENDED CLI CHANGE")
    print(f"  Replace `--calibrated-stake-scale-mode shadow` with:")
    print(f"      {cli_change}")
    print()
    print(f"  Note: this promote ALSO writes `calibrated_stake_scale_mode=enforce` to")
    print(f"  {args.live_overrides_path}, which the live engine reads on startup.")
    print(f"  Explicit CLI flag still wins if you pass one.")

    if args.dry_run:
        print("\nDRY-RUN no overrides written, no event recorded.")
        write_promotion_event(
            PromotionEvent(
                lever="stake_scaling",
                side=getattr(args, "side", "over"),
                action="dry_run",
                operator=operator,
                verdict_snapshot=payload,
                from_state={"calibrated_stake_scale_mode": "shadow"},
                to_state={"calibrated_stake_scale_mode": "enforce"},
            ),
            log_path=args.event_log_path,
        )
        return 0

    backup_path: Optional[Path] = None
    try:
        backup_path, _payload = _live_overrides_set(
            path=args.live_overrides_path,
            operator=operator,
            top_level={"calibrated_stake_scale_mode": "enforce"},
        )
    except (OSError, ValueError) as exc:
        msg = f"failed to write overrides file: {exc!r}"
        print(f"\nERROR {msg}", file=sys.stderr)
        write_promotion_event(
            PromotionEvent(
                lever="stake_scaling",
                side=getattr(args, "side", "over"),
                action="blocked",
                operator=operator,
                verdict_snapshot=payload,
                block_reason=msg,
            ),
            log_path=args.event_log_path,
        )
        return 2

    write_promotion_event(
        PromotionEvent(
            lever="stake_scaling",
            side=getattr(args, "side", "over"),
            action="forced" if (verdict_label != "promote" and args.force) else "promoted",
            operator=operator,
            verdict_snapshot=payload,
            from_state={"calibrated_stake_scale_mode": "shadow"},
            to_state={
                "calibrated_stake_scale_mode": "enforce",
                "overrides_path": str(args.live_overrides_path),
            },
            backup_path=str(backup_path) if backup_path else None,
            source_artifact_lineage=_capture_artifact_lineage(args.stake_scaling_report_path),
            promotion_lineage=_compute_promotion_lineage(),
            notes="overrides file mutated; restart live engine to pick up",
        ),
        log_path=args.event_log_path,
    )
    _print_checklist(
        [
            f"Restart `live_engine.py` so it re-reads {args.live_overrides_path}.",
            "Next live session will multiply base stake by `calibrated_stake_multiplier` "
            "(was logged-only in shadow).",
            "Continue watching `analyze_stake_scaling_promotion`'s verdict daily; "
            "if the high-vs-low gap collapses, run `promote.py demote stake-scaling`.",
            f"Audit trail: see {args.event_log_path} for this promotion's row.",
        ]
    )
    return 0


# ---------------------------------------------------------------------------
# `gate-threshold` subcommand: change a per-gate threshold based on
# walk-forward certification's RETUNE recommendation.
# ---------------------------------------------------------------------------


def cmd_gate_threshold(args: argparse.Namespace) -> int:
    _print_header(f"Promote gate-threshold change: {args.gate_name} -> {args.new_value}")
    operator = _resolve_operator(args.operator)
    payload, err = _safe_load_json(args.walk_forward_cert_path)
    if err or not isinstance(payload, dict):
        print(
            f"\nERROR could not read walk-forward certification at "
            f"{args.walk_forward_cert_path}: {err}",
            file=sys.stderr,
        )
        write_promotion_event(
            PromotionEvent(
                lever="gate_threshold",
                side=getattr(args, "side", "over"),
                action="blocked",
                operator=operator,
                block_reason=f"walk-forward cert unreadable: {err}",
            ),
            log_path=args.event_log_path,
        )
        return 2

    gates = payload.get("gates") or []
    gate_entry = next(
        (g for g in gates if (g.get("name") or "") == args.gate_name), None
    )
    if gate_entry is None:
        msg = f"gate '{args.gate_name}' not found in walk-forward certification"
        print(f"\nERROR {msg}", file=sys.stderr)
        print(
            "  Available gates: "
            + ", ".join(g.get("name") or "?" for g in gates)
        )
        write_promotion_event(
            PromotionEvent(
                lever="gate_threshold",
                side=getattr(args, "side", "over"),
                action="blocked",
                operator=operator,
                block_reason=msg,
            ),
            log_path=args.event_log_path,
        )
        return 2

    gate_verdict = gate_entry.get("verdict") or {}
    verdict_label = str(gate_verdict.get("verdict") or "").upper()
    current_threshold = gate_entry.get("current_threshold")
    recommended = gate_verdict.get("recommended_threshold")
    readiness = (payload.get("readiness") or {}).get("label", "<unknown>")
    _print_block("readiness", readiness)
    _print_block("gate verdict", verdict_label)
    _print_block("current threshold", current_threshold)
    _print_block("recommended threshold", recommended)
    _print_block("reason", (gate_verdict.get("reason") or "").strip()[:200])

    if verdict_label not in ("RETUNE", "RETIRE") and not args.force:
        msg = (
            f"gate verdict is '{verdict_label}', not 'RETUNE' / 'RETIRE'; "
            "refusing to recommend change (use --force to override)"
        )
        print(f"\nBLOCKED {msg}")
        write_promotion_event(
            PromotionEvent(
                lever="gate_threshold",
                side=getattr(args, "side", "over"),
                action="blocked",
                operator=operator,
                verdict_snapshot={
                    "gate_name": args.gate_name,
                    "gate_verdict": gate_verdict,
                    "current_threshold": current_threshold,
                    "readiness": readiness,
                },
                block_reason=msg,
            ),
            log_path=args.event_log_path,
        )
        return 1

    # Map gate name to the live-engine CLI flag. Centralizing here so the
    # promote CLI is the one place a future agent maintains the binding.
    flag = _gate_cli_flag(args.gate_name)
    if flag is None:
        msg = (
            f"gate '{args.gate_name}' has no documented CLI flag mapping; "
            "extend `_gate_cli_flag` in promote.py before retuning."
        )
        print(f"\nERROR {msg}", file=sys.stderr)
        write_promotion_event(
            PromotionEvent(
                lever="gate_threshold",
                side=getattr(args, "side", "over"),
                action="blocked",
                operator=operator,
                verdict_snapshot={"gate_name": args.gate_name, "gate_verdict": gate_verdict},
                block_reason=msg,
            ),
            log_path=args.event_log_path,
        )
        return 2

    print(f"\nRECOMMENDED CLI CHANGE")
    print(f"  Update the live-engine flag:")
    print(f"      {flag} {args.new_value}")
    print(f"  (replacing the prior value; current threshold is {current_threshold})")
    print()
    print(f"  Note: this promote ALSO writes gate_thresholds.{args.gate_name}={args.new_value}")
    print(f"  to {args.live_overrides_path}; live engine reads it on startup.")

    if args.dry_run:
        print("\nDRY-RUN no overrides written, no event recorded.")
        write_promotion_event(
            PromotionEvent(
                lever="gate_threshold",
                side=getattr(args, "side", "over"),
                action="dry_run",
                operator=operator,
                verdict_snapshot={
                    "gate_name": args.gate_name,
                    "gate_verdict": gate_verdict,
                    "readiness": readiness,
                },
                from_state={"threshold": current_threshold},
                to_state={"threshold": args.new_value, "cli_flag": flag},
            ),
            log_path=args.event_log_path,
        )
        return 0

    backup_path: Optional[Path] = None
    try:
        typed_value = _parse_gate_value(args.gate_name, args.new_value)
        backup_path, _payload = _live_overrides_set(
            path=args.live_overrides_path,
            operator=operator,
            gate_thresholds={args.gate_name: typed_value},
        )
    except (OSError, ValueError) as exc:
        msg = f"failed to write overrides file: {exc!r}"
        print(f"\nERROR {msg}", file=sys.stderr)
        write_promotion_event(
            PromotionEvent(
                lever="gate_threshold",
                side=getattr(args, "side", "over"),
                action="blocked",
                operator=operator,
                verdict_snapshot={
                    "gate_name": args.gate_name,
                    "gate_verdict": gate_verdict,
                    "readiness": readiness,
                },
                block_reason=msg,
            ),
            log_path=args.event_log_path,
        )
        return 2

    write_promotion_event(
        PromotionEvent(
            lever="gate_threshold",
            side=getattr(args, "side", "over"),
            action="forced" if (verdict_label not in ("RETUNE", "RETIRE") and args.force) else "promoted",
            operator=operator,
            verdict_snapshot={
                "gate_name": args.gate_name,
                "gate_verdict": gate_verdict,
                "readiness": readiness,
            },
            from_state={"threshold": current_threshold},
            to_state={
                "threshold": args.new_value,
                "cli_flag": flag,
                "overrides_path": str(args.live_overrides_path),
            },
            backup_path=str(backup_path) if backup_path else None,
            source_artifact_lineage=_capture_artifact_lineage(args.walk_forward_cert_path),
            promotion_lineage=_compute_promotion_lineage(),
            notes="overrides file mutated; restart live engine to pick up",
        ),
        log_path=args.event_log_path,
    )
    _print_checklist(
        [
            f"Restart `live_engine.py` so it re-reads {args.live_overrides_path}.",
            f"Next live session will enforce {args.gate_name} at {args.new_value}.",
            "Re-check walk-forward certification after ~7 sessions: the gate's blocked-cohort "
            "ROI should converge as the new threshold takes effect.",
            f"Audit trail: see {args.event_log_path} for this promotion's row.",
        ]
    )
    return 0


# Centralised gate -> CLI flag mapping. Keep in sync with
# `live_engine_cli.py`'s argparse definitions.
_GATE_CLI_FLAGS: Dict[str, str] = {
    "gate_extreme_edge": "--extreme-edge-max",
    "gate_min_edge": "--edge-threshold",
    "gate_min_inning": "--min-inning",
    "gate_min_entry_ask": "--min-entry-ask",
    "gate_runs_needed_max": "--runs-needed-max",
}

# Per-gate value type. Used when writing to the runtime overrides file so
# `live_engine_cli.py`'s argparse-typed targets receive the right type
# (extreme_edge_max is float; min_inning is int). Keep in sync with
# signal_config.py argparse definitions.
_GATE_VALUE_TYPE: Dict[str, type] = {
    "gate_extreme_edge": float,
    "gate_min_edge": float,
    "gate_min_inning": int,
    "gate_min_entry_ask": float,
    "gate_runs_needed_max": float,
}


def _gate_cli_flag(gate_name: str) -> Optional[str]:
    return _GATE_CLI_FLAGS.get(gate_name)


def _parse_gate_value(gate_name: str, raw: Any) -> Any:
    """Coerce a raw gate-threshold value (typically a CLI-supplied string)
    to the type the live engine expects. Returns the raw value unchanged
    when the gate isn't in `_GATE_VALUE_TYPE` (defensive: the caller
    has already validated the gate name via `_gate_cli_flag`)."""
    target = _GATE_VALUE_TYPE.get(gate_name)
    if target is None or isinstance(raw, target):
        return raw
    return target(raw)


# ---------------------------------------------------------------------------
# `demote` subcommands. Symmetric mirror of the four promote subcommands.
# Each reads its lever's demotion verdict, refuses unless verdict says go
# (or --force), performs the inverse action (file restore for stage2/
# stage3-v2; CLI-flag revert recommendation for stake-scaling/gate-threshold),
# and appends a row to the audit log with direction="demote".
# ---------------------------------------------------------------------------


def _print_demotion_verdict_block(verdict: Dict[str, Any]) -> None:
    _print_block("verdict", verdict["verdict"])
    pre = verdict.get("pre_window") or {}
    post = verdict.get("post_window") or {}
    if pre:
        _print_block(
            "pre window",
            f"n={pre.get('n_filled', 0)}  W/L={pre.get('wins', 0)}/{pre.get('losses', 0)}  "
            f"ROI={(pre.get('roi') or 0) * 100:+.1f}%  "
            f"profit=${pre.get('total_profit', 0):+.2f}",
        )
    if post:
        _print_block(
            "post window",
            f"n={post.get('n_filled', 0)}  W/L={post.get('wins', 0)}/{post.get('losses', 0)}  "
            f"ROI={(post.get('roi') or 0) * 100:+.1f}%  "
            f"profit=${post.get('total_profit', 0):+.2f}",
        )
    if verdict.get("roi_delta") is not None:
        _print_block("ROI delta", f"{verdict['roi_delta'] * 100:+.1f}pp (threshold: <= {verdict['regression_threshold'] * 100:+.0f}pp)")
    pe = verdict.get("promotion_event") or {}
    if pe:
        _print_block("promoted at", pe.get("generated_at_utc"))
        _print_block("promoted by", pe.get("operator"))


def _demote_verdict_gate(
    *,
    verdict: Dict[str, Any],
    args: argparse.Namespace,
    operator: str,
    lever: str,
) -> Optional[int]:
    """Common gate logic: refuse unless verdict=='demote' (or --force).
    Returns an exit code if the call should abort; None if it should
    proceed. Writes the appropriate audit row in either case."""
    label = verdict.get("verdict")
    if label == "no_promotion_to_demote" and not args.force:
        msg = "no recent promotion to demote (audit log has no promote/forced event for this lever)"
        print(f"\nBLOCKED {msg}")
        write_promotion_event(
            PromotionEvent(
                lever=lever,
                action="blocked",
                direction="demote",
                operator=operator,
                verdict_snapshot=verdict,
                block_reason=msg,
            ),
            log_path=args.event_log_path,
        )
        return 1
    # Either "demote" (windowed verdict) or "fast_demote" (Wilson UB)
    # is sufficient to proceed. The two checks are independent; firing
    # either means we have actionable evidence the policy is failing.
    if label not in {"demote", "fast_demote"} and not args.force:
        msg = (
            f"verdict is '{label}', not 'demote'/'fast_demote'; "
            "refusing to demote (use --force to override)"
        )
        print(f"\nBLOCKED {msg}")
        write_promotion_event(
            PromotionEvent(
                lever=lever,
                action="blocked",
                direction="demote",
                operator=operator,
                verdict_snapshot=verdict,
                block_reason=msg,
            ),
            log_path=args.event_log_path,
        )
        return 1
    return None


def cmd_demote_stage2(args: argparse.Namespace) -> int:
    _print_header("Demote Stage-2: restore prior production cache from backup")
    operator = _resolve_operator(args.operator)
    events = load_promotion_events(args.event_log_path)
    verdict = stage2_demotion_verdict(
        events=events, sessions_dir=args.sessions_dir,
    )
    _print_demotion_verdict_block(verdict)

    gate = _demote_verdict_gate(
        verdict=verdict, args=args, operator=operator, lever="stage2",
    )
    if gate is not None:
        return gate

    pe = verdict.get("promotion_event") or {}
    backup_str = pe.get("backup_path")
    backup_path: Optional[Path] = Path(backup_str) if backup_str else None

    if args.dry_run:
        if backup_path:
            print(
                f"\nDRY-RUN would atomically restore "
                f"{backup_path.name} -> {args.stage2_cache_path.name}"
            )
        else:
            print(
                f"\nDRY-RUN would delete {args.stage2_cache_path.name} "
                "(no prior backup; runtime would fall back to load default)"
            )
        write_promotion_event(
            PromotionEvent(
                lever="stage2",
                action="dry_run",
                direction="demote",
                operator=operator,
                verdict_snapshot=verdict,
                from_state={"production_path": str(args.stage2_cache_path)},
                to_state={"restored_from": str(backup_path) if backup_path else None},
            ),
            log_path=args.event_log_path,
        )
        return 0

    # Restore: atomic copy backup -> production. If backup is missing
    # (first-promotion case or backup file lost), the safe action is to
    # delete the current production file so the next refresh's staging
    # rebuild + sanity guard re-promotes from scratch.
    try:
        if backup_path and backup_path.exists():
            _atomic_copy(backup_path, args.stage2_cache_path)
            print(f"\nDEMOTED Stage-2: restored {backup_path.name} -> {args.stage2_cache_path.name}")
        else:
            if args.stage2_cache_path.exists():
                args.stage2_cache_path.unlink()
            print(
                f"\nDEMOTED Stage-2: removed {args.stage2_cache_path.name} "
                "(no backup found; next refresh's stage1_cache_promote will rebuild)"
            )
    except OSError as exc:
        print(f"\nERROR demote file action failed: {exc!r}", file=sys.stderr)
        write_promotion_event(
            PromotionEvent(
                lever="stage2",
                action="blocked",
                direction="demote",
                operator=operator,
                verdict_snapshot=verdict,
                block_reason=f"file action failed: {exc!r}",
            ),
            log_path=args.event_log_path,
        )
        return 3

    write_promotion_event(
        PromotionEvent(
            lever="stage2",
            action="forced" if (verdict.get("verdict") != "demote" and args.force) else "demoted",
            direction="demote",
            operator=operator,
            verdict_snapshot=verdict,
            from_state={"production_path": str(args.stage2_cache_path)},
            to_state={"restored_from": str(backup_path) if backup_path else None},
        ),
        log_path=args.event_log_path,
    )
    _print_checklist(
        [
            "Restart `live_engine.py` (or wait for next session) so the restored Stage-2 cache loads.",
            "If outcomes recover, the demote was correct. If not, investigate further upstream.",
            f"Audit trail: see {args.event_log_path} for this demotion's row.",
        ]
    )
    return 0


def cmd_demote_stage3_v2(args: argparse.Namespace) -> int:
    _print_header("Demote Stage-3 v2: restore prior production weights from backup")
    operator = _resolve_operator(args.operator)
    events = load_promotion_events(args.event_log_path)
    verdict = stage3_v2_demotion_verdict(
        events=events, sessions_dir=args.sessions_dir,
    )
    _print_demotion_verdict_block(verdict)

    gate = _demote_verdict_gate(
        verdict=verdict, args=args, operator=operator, lever="stage3_v2",
    )
    if gate is not None:
        return gate

    pe = verdict.get("promotion_event") or {}
    backup_str = pe.get("backup_path")
    backup_path: Optional[Path] = Path(backup_str) if backup_str else None
    prod_path = args.stage3_v2_prod_weights_path

    if args.dry_run:
        if backup_path:
            print(f"\nDRY-RUN would restore {backup_path.name} -> {prod_path.name}")
        else:
            print(
                f"\nDRY-RUN would delete {prod_path.name} "
                "(no prior backup; runtime would fall back to compiled defaults)"
            )
        write_promotion_event(
            PromotionEvent(
                lever="stage3_v2",
                action="dry_run",
                direction="demote",
                operator=operator,
                verdict_snapshot=verdict,
                from_state={"production_path": str(prod_path)},
                to_state={"restored_from": str(backup_path) if backup_path else "compiled_defaults"},
            ),
            log_path=args.event_log_path,
        )
        return 0

    try:
        if backup_path and backup_path.exists():
            _atomic_copy(backup_path, prod_path)
            print(f"\nDEMOTED Stage-3 v2: restored {backup_path.name} -> {prod_path.name}")
        else:
            if prod_path.exists():
                prod_path.unlink()
            print(
                f"\nDEMOTED Stage-3 v2: removed {prod_path.name} "
                "(no backup found; runtime falls back to compiled defaults)"
            )
    except OSError as exc:
        print(f"\nERROR demote file action failed: {exc!r}", file=sys.stderr)
        write_promotion_event(
            PromotionEvent(
                lever="stage3_v2",
                action="blocked",
                direction="demote",
                operator=operator,
                verdict_snapshot=verdict,
                block_reason=f"file action failed: {exc!r}",
            ),
            log_path=args.event_log_path,
        )
        return 3

    write_promotion_event(
        PromotionEvent(
            lever="stage3_v2",
            action="forced" if (verdict.get("verdict") != "demote" and args.force) else "demoted",
            direction="demote",
            operator=operator,
            verdict_snapshot=verdict,
            from_state={"production_path": str(prod_path)},
            to_state={"restored_from": str(backup_path) if backup_path else "compiled_defaults"},
        ),
        log_path=args.event_log_path,
    )
    _print_checklist(
        [
            "Restart `live_engine.py` so TeamOffenseModel re-loads (will use restored weights, or compiled defaults if no backup).",
            "Verify startup log line: 'TeamOffenseModel loaded ... betas=...' matches expected.",
            f"Audit trail: see {args.event_log_path} for this demotion's row.",
        ]
    )
    return 0


def cmd_demote_stake_scaling(args: argparse.Namespace) -> int:
    _print_header("Demote stake-scaling: enforce -> shadow")
    operator = _resolve_operator(args.operator)
    events = load_promotion_events(args.event_log_path)
    verdict = stake_scaling_demotion_verdict(
        events=events, sessions_dir=args.sessions_dir,
    )
    _print_demotion_verdict_block(verdict)

    gate = _demote_verdict_gate(
        verdict=verdict, args=args, operator=operator, lever="stake_scaling",
    )
    if gate is not None:
        return gate

    cli_change = "--calibrated-stake-scale-mode shadow"
    print(f"\nRECOMMENDED CLI CHANGE")
    print(f"  Replace `--calibrated-stake-scale-mode enforce` with:")
    print(f"      {cli_change}")
    print(f"  In the live trader command line.")
    print()
    print(f"  Note: this demote ALSO removes `calibrated_stake_scale_mode` from")
    print(f"  {args.live_overrides_path} (or restores from backup if one exists).")

    if args.dry_run:
        print("\nDRY-RUN no overrides changed, no event recorded.")
        write_promotion_event(
            PromotionEvent(
                lever="stake_scaling",
                side=getattr(args, "side", "over"),
                action="dry_run",
                direction="demote",
                operator=operator,
                verdict_snapshot=verdict,
                from_state={"calibrated_stake_scale_mode": "enforce"},
                to_state={"calibrated_stake_scale_mode": "shadow"},
            ),
            log_path=args.event_log_path,
        )
        return 0

    # Demote: drop the override key. Restore from backup if one exists
    # (a prior promote->demote->repromote cycle may have stacked
    # backups; remove_override captures the current state into a fresh
    # backup so a subsequent re-promote can also be reverted).
    backup_path: Optional[Path] = None
    try:
        backup_path, _payload = _live_overrides_remove(
            path=args.live_overrides_path,
            operator=operator,
            top_level_keys=["calibrated_stake_scale_mode"],
        )
    except OSError as exc:
        msg = f"failed to update overrides file: {exc!r}"
        print(f"\nERROR {msg}", file=sys.stderr)
        write_promotion_event(
            PromotionEvent(
                lever="stake_scaling",
                side=getattr(args, "side", "over"),
                action="blocked",
                direction="demote",
                operator=operator,
                verdict_snapshot=verdict,
                block_reason=msg,
            ),
            log_path=args.event_log_path,
        )
        return 2

    write_promotion_event(
        PromotionEvent(
            lever="stake_scaling",
            side=getattr(args, "side", "over"),
            action="forced" if (verdict.get("verdict") != "demote" and args.force) else "demoted",
            direction="demote",
            operator=operator,
            verdict_snapshot=verdict,
            from_state={"calibrated_stake_scale_mode": "enforce"},
            to_state={
                "calibrated_stake_scale_mode": "shadow",
                "overrides_path": str(args.live_overrides_path),
            },
            backup_path=str(backup_path) if backup_path else None,
            notes="overrides key removed; restart live engine to pick up",
        ),
        log_path=args.event_log_path,
    )
    _print_checklist(
        [
            f"Restart `live_engine.py` so it re-reads {args.live_overrides_path}.",
            "Multiplier will continue to be computed and logged but won't change stake.",
            "Continue watching `analyze_stake_scaling_promotion`'s verdict daily.",
            f"Audit trail: see {args.event_log_path} for this demotion's row.",
        ]
    )
    return 0


def cmd_demote_gate_threshold(args: argparse.Namespace) -> int:
    _print_header(f"Demote gate-threshold: revert {args.gate_name}")
    operator = _resolve_operator(args.operator)
    events = load_promotion_events(args.event_log_path)
    verdict = gate_threshold_demotion_verdict(
        events=events, sessions_dir=args.sessions_dir,
    )
    _print_demotion_verdict_block(verdict)

    # The lever-level promotion event isn't gate-name-specific (we log
    # all gate-threshold changes under lever="gate_threshold"); the
    # promotion event's from_state.threshold IS the value to revert to.
    pe = verdict.get("promotion_event") or {}
    from_state = pe.get("from_state") or {}
    prior_threshold = from_state.get("threshold")
    if prior_threshold is None and not args.force:
        msg = "promotion event has no prior threshold to revert to (--force to specify --to-value)"
        print(f"\nBLOCKED {msg}")
        write_promotion_event(
            PromotionEvent(
                lever="gate_threshold",
                side=getattr(args, "side", "over"),
                action="blocked",
                direction="demote",
                operator=operator,
                verdict_snapshot=verdict,
                block_reason=msg,
            ),
            log_path=args.event_log_path,
        )
        return 1

    gate = _demote_verdict_gate(
        verdict=verdict, args=args, operator=operator, lever="gate_threshold",
    )
    if gate is not None:
        return gate

    flag = _gate_cli_flag(args.gate_name)
    if flag is None:
        msg = (
            f"gate '{args.gate_name}' has no CLI flag mapping; "
            "extend `_gate_cli_flag` in promote.py before reverting."
        )
        print(f"\nERROR {msg}", file=sys.stderr)
        write_promotion_event(
            PromotionEvent(
                lever="gate_threshold",
                side=getattr(args, "side", "over"),
                action="blocked",
                direction="demote",
                operator=operator,
                verdict_snapshot=verdict,
                block_reason=msg,
            ),
            log_path=args.event_log_path,
        )
        return 2

    revert_value = args.to_value if args.to_value is not None else prior_threshold
    print(f"\nRECOMMENDED CLI CHANGE")
    print(f"  Update the live-engine flag back to:")
    print(f"      {flag} {revert_value}")
    print(f"  (replacing the post-promotion value)")
    print()
    print(f"  Note: this demote ALSO removes gate_thresholds.{args.gate_name}")
    print(f"  from {args.live_overrides_path}. With `--to-value` set, the override")
    print(f"  is instead replaced with the explicit revert value.")

    if args.dry_run:
        print("\nDRY-RUN no overrides changed, no event recorded.")
        write_promotion_event(
            PromotionEvent(
                lever="gate_threshold",
                side=getattr(args, "side", "over"),
                action="dry_run",
                direction="demote",
                operator=operator,
                verdict_snapshot=verdict,
                from_state={"gate_name": args.gate_name},
                to_state={"threshold": revert_value, "cli_flag": flag},
            ),
            log_path=args.event_log_path,
        )
        return 0

    backup_path: Optional[Path] = None
    try:
        if args.to_value is not None:
            # Operator-specified revert value: write it as the new override.
            typed_value = _parse_gate_value(args.gate_name, args.to_value)
            backup_path, _payload = _live_overrides_set(
                path=args.live_overrides_path,
                operator=operator,
                gate_thresholds={args.gate_name: typed_value},
            )
        else:
            # Default revert: drop the override key so the engine falls
            # back to argparse defaults (or the prior_threshold the
            # operator's saved command supplies).
            backup_path, _payload = _live_overrides_remove(
                path=args.live_overrides_path,
                operator=operator,
                gate_threshold_keys=[args.gate_name],
            )
    except (OSError, ValueError) as exc:
        msg = f"failed to update overrides file: {exc!r}"
        print(f"\nERROR {msg}", file=sys.stderr)
        write_promotion_event(
            PromotionEvent(
                lever="gate_threshold",
                side=getattr(args, "side", "over"),
                action="blocked",
                direction="demote",
                operator=operator,
                verdict_snapshot=verdict,
                block_reason=msg,
            ),
            log_path=args.event_log_path,
        )
        return 2

    write_promotion_event(
        PromotionEvent(
            lever="gate_threshold",
            side=getattr(args, "side", "over"),
            action="forced" if (verdict.get("verdict") != "demote" and args.force) else "demoted",
            direction="demote",
            operator=operator,
            verdict_snapshot=verdict,
            from_state={"gate_name": args.gate_name},
            to_state={
                "threshold": revert_value,
                "cli_flag": flag,
                "overrides_path": str(args.live_overrides_path),
            },
            backup_path=str(backup_path) if backup_path else None,
            notes="overrides file mutated; restart live engine to pick up",
        ),
        log_path=args.event_log_path,
    )
    _print_checklist(
        [
            f"Restart `live_engine.py` so it re-reads {args.live_overrides_path}.",
            f"Next live session will enforce {args.gate_name} at {revert_value}.",
            f"Audit trail: see {args.event_log_path} for this demotion's row.",
        ]
    )
    return 0


# ---------------------------------------------------------------------------
# CLI parser + dispatch
# ---------------------------------------------------------------------------


def _add_common_flags(p: argparse.ArgumentParser) -> None:
    p.add_argument("--dry-run", action="store_true", help="Print planned action without performing it.")
    p.add_argument(
        "--force",
        action="store_true",
        help="Proceed even when the verdict is not 'promote'/'RETUNE'. Logs action as 'forced'.",
    )
    p.add_argument("--operator", type=str, default=None, help="Operator label for the event log (defaults to $USER).")
    p.add_argument(
        "--event-log-path",
        type=Path,
        default=DEFAULT_PROMOTION_EVENTS_LOG,
        help="Path to promotion events log (append-only JSONL).",
    )


def _add_side_flag(p: argparse.ArgumentParser) -> None:
    """Add --side {over,under,both} for levers whose effect is side-asymmetric.

    Phase B B2 (2026-05-16). stage2 + stage3-v2 are side-symmetric so
    they don't get this flag (the audit row hard-codes side='both').
    stake-scaling and gate-threshold flip the live engine's runtime
    behavior on the Over side today; Phase C will add Under counterparts
    that record side='under'.
    """
    p.add_argument(
        "--side",
        choices=["over", "under", "both"],
        default="over",
        help=(
            "Which side this promotion affects. Defaults to 'over' for "
            "today's levers (the live engine is Over-only). Phase C "
            "introduces UNDER actuation; once those land, operators "
            "pass --side under explicitly. The audit log carries this "
            "field so daemon retrospective + drift attribution can "
            "filter by side."
        ),
    )


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="promote.py",
        description="Unified promotion CLI for the four manual self-improvement levers.",
    )
    sub = p.add_subparsers(dest="lever", required=True)

    # status
    p_status = sub.add_parser("status", help="Read all four verdicts and print one summary.")
    p_status.add_argument(
        "--stage2-brier-history-path",
        type=Path,
        default=DEFAULT_STAGE2_BRIER_HISTORY_PATH,
    )
    p_status.add_argument(
        "--stage3-v2-drift-history-path",
        type=Path,
        default=DEFAULT_STAGE3_V2_DRIFT_HISTORY_PATH,
    )
    p_status.add_argument(
        "--stake-scaling-report-path",
        type=Path,
        default=DEFAULT_STAKE_SCALING_REPORT_PATH,
    )
    p_status.add_argument(
        "--walk-forward-cert-path",
        type=Path,
        default=DEFAULT_WALK_FORWARD_CERT_PATH,
    )
    # Status also reads the audit log + session bets to compute demotion
    # verdicts (post-promotion outcome regression). Defaults match the
    # demote subcommands so behaviour is consistent across the surface.
    p_status.add_argument(
        "--event-log-path",
        type=Path,
        default=DEFAULT_PROMOTION_EVENTS_LOG,
    )
    p_status.add_argument(
        "--sessions-dir",
        type=Path,
        default=DEFAULT_SESSIONS_DIR,
    )
    p_status.set_defaults(handler=cmd_status)

    # stage2
    p_s2 = sub.add_parser("stage2", help="Promote Stage-2 staging cache -> production cache.")
    _add_common_flags(p_s2)
    p_s2.add_argument(
        "--stage2-brier-history-path",
        type=Path,
        default=DEFAULT_STAGE2_BRIER_HISTORY_PATH,
    )
    p_s2.add_argument(
        "--stage2-staging-path",
        type=Path,
        default=DEFAULT_STAGE2_STAGING_PATH,
    )
    p_s2.add_argument(
        "--stage2-cache-path",
        type=Path,
        default=DEFAULT_STAGE2_CACHE_PATH,
    )
    p_s2.set_defaults(handler=cmd_stage2)

    # stage3-v2
    p_s3 = sub.add_parser("stage3-v2", help="Promote Stage-3 v2 research fit -> production weights.")
    _add_common_flags(p_s3)
    p_s3.add_argument(
        "--stage3-v2-drift-history-path",
        type=Path,
        default=DEFAULT_STAGE3_V2_DRIFT_HISTORY_PATH,
    )
    p_s3.add_argument(
        "--stage3-v2-research-fit-path",
        type=Path,
        default=DEFAULT_STAGE3_V2_RESEARCH_FIT_PATH,
    )
    p_s3.add_argument(
        "--stage3-v2-prod-weights-path",
        type=Path,
        default=DEFAULT_STAGE3_V2_PROD_WEIGHTS_PATH,
    )
    p_s3.add_argument(
        "--promote-team-offense-script",
        type=Path,
        default=DEFAULT_PROMOTE_TEAM_OFFENSE_SCRIPT,
    )
    p_s3.set_defaults(handler=cmd_stage3_v2)

    # stake-scaling
    p_ss = sub.add_parser("stake-scaling", help="Promote stake-scaling shadow -> enforce.")
    _add_common_flags(p_ss)
    _add_side_flag(p_ss)
    p_ss.add_argument(
        "--stake-scaling-report-path",
        type=Path,
        default=DEFAULT_STAKE_SCALING_REPORT_PATH,
    )
    p_ss.add_argument(
        "--live-overrides-path",
        type=Path,
        default=DEFAULT_LIVE_ENGINE_OVERRIDES_PATH,
        help="Runtime overrides file the live engine reads on startup.",
    )
    p_ss.set_defaults(handler=cmd_stake_scaling)

    # gate-threshold
    p_gt = sub.add_parser(
        "gate-threshold",
        help="Change a per-gate threshold based on walk-forward certification's RETUNE.",
    )
    _add_common_flags(p_gt)
    _add_side_flag(p_gt)
    p_gt.add_argument("gate_name", help="Gate name (e.g. gate_extreme_edge, gate_min_edge).")
    p_gt.add_argument("new_value", help="New threshold value (passed as-is to the CLI flag).")
    p_gt.add_argument(
        "--walk-forward-cert-path",
        type=Path,
        default=DEFAULT_WALK_FORWARD_CERT_PATH,
    )
    p_gt.add_argument(
        "--live-overrides-path",
        type=Path,
        default=DEFAULT_LIVE_ENGINE_OVERRIDES_PATH,
        help="Runtime overrides file the live engine reads on startup.",
    )
    p_gt.set_defaults(handler=cmd_gate_threshold)

    # ---- demote: nested subcommand parser, mirrors promote shape ----
    p_demote = sub.add_parser(
        "demote",
        help="Roll back a prior promotion (mirror of promote subcommands).",
    )
    demote_sub = p_demote.add_subparsers(dest="demote_lever", required=True)

    p_d_s2 = demote_sub.add_parser("stage2", help="Restore prior Stage-2 production cache from backup.")
    _add_common_flags(p_d_s2)
    p_d_s2.add_argument(
        "--stage2-cache-path",
        type=Path,
        default=DEFAULT_STAGE2_CACHE_PATH,
    )
    p_d_s2.add_argument(
        "--sessions-dir",
        type=Path,
        default=DEFAULT_SESSIONS_DIR,
        help="Live sessions directory (read for pre/post-promotion ROI comparison).",
    )
    p_d_s2.set_defaults(handler=cmd_demote_stage2)

    p_d_s3 = demote_sub.add_parser("stage3-v2", help="Restore prior Stage-3 v2 production weights from backup.")
    _add_common_flags(p_d_s3)
    p_d_s3.add_argument(
        "--stage3-v2-prod-weights-path",
        type=Path,
        default=DEFAULT_STAGE3_V2_PROD_WEIGHTS_PATH,
    )
    p_d_s3.add_argument(
        "--sessions-dir",
        type=Path,
        default=DEFAULT_SESSIONS_DIR,
    )
    p_d_s3.set_defaults(handler=cmd_demote_stage3_v2)

    p_d_ss = demote_sub.add_parser("stake-scaling", help="Demote stake-scaling enforce -> shadow.")
    _add_common_flags(p_d_ss)
    _add_side_flag(p_d_ss)
    p_d_ss.add_argument(
        "--sessions-dir",
        type=Path,
        default=DEFAULT_SESSIONS_DIR,
    )
    p_d_ss.add_argument(
        "--live-overrides-path",
        type=Path,
        default=DEFAULT_LIVE_ENGINE_OVERRIDES_PATH,
        help="Runtime overrides file the live engine reads on startup.",
    )
    p_d_ss.set_defaults(handler=cmd_demote_stake_scaling)

    p_d_gt = demote_sub.add_parser("gate-threshold", help="Revert a per-gate threshold change.")
    _add_common_flags(p_d_gt)
    _add_side_flag(p_d_gt)
    p_d_gt.add_argument("gate_name", help="Gate name (e.g. gate_extreme_edge).")
    p_d_gt.add_argument(
        "--to-value", type=str, default=None,
        help="Threshold to revert to. Default: prior_threshold from the promotion event's from_state.",
    )
    p_d_gt.add_argument(
        "--sessions-dir",
        type=Path,
        default=DEFAULT_SESSIONS_DIR,
    )
    p_d_gt.add_argument(
        "--live-overrides-path",
        type=Path,
        default=DEFAULT_LIVE_ENGINE_OVERRIDES_PATH,
        help="Runtime overrides file the live engine reads on startup.",
    )
    p_d_gt.set_defaults(handler=cmd_demote_gate_threshold)

    return p.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    handler = getattr(args, "handler", None)
    if handler is None:
        print("no handler attached to args", file=sys.stderr)
        return 64
    return int(handler(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
