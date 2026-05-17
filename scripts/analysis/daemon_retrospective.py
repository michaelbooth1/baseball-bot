#!/usr/bin/env python3
"""daemon_retrospective.py -- replay the auto-promote/demote daemon's
decision logic against historical data, then compare what it WOULD
have done vs what the operator actually did.

This is the evidence path operators use to gain confidence before
flipping `--auto-daemon-mode` from `preview` to `act`. The daemon's
decisions for each date in history are reconstructed from the
per-date history snapshots (`stage2_brier_history.jsonl`,
`stage3_v2_drift_history.jsonl`) plus the audit log
(`promote_events.jsonl`); for each (date, lever) the agreement
between daemon and operator is classified into one of:

  - MATCH              -- daemon would have acted AND operator acted
                          same direction
  - DAEMON_ONLY        -- daemon would have acted, operator did not
                          (potential over-action by daemon)
  - OPERATOR_ONLY      -- operator acted, daemon would not have
                          (potential miss by daemon)
  - DAEMON_DISAGREED   -- daemon would have promoted but operator
                          demoted (or vice versa) -- highest severity
  - BOTH_NO_ACTION     -- both correctly did nothing

Readiness verdict per lever:
  - ready_for_act        -- >= 7 dates evaluated, zero disagreements
                            AND zero daemon-only over-acts
  - needs_more_history   -- < 7 dates evaluated
  - disagreements_present -- any DAEMON_DISAGREED or DAEMON_ONLY

Scope of v1:
  - Promote-decision replay only for `stage2` and `stage3-v2` (the
    two levers with proper per-date history files).
  - Snapshot-only for `stake-scaling` and `gate-threshold` (these
    have point-in-time verdict files, not per-date history; for
    point-in-time verdicts we can't reconstruct yesterday's
    snapshot from disk).
  - Demote-decision replay is deferred -- the demotion verdict
    needs pre/post session windows that only ratify after the
    promotion event's post-window matures (~14 days). Sample size
    today (one promotion event total in the live audit log) makes
    demote retrospective trivial; the script will be more useful
    on this dimension after the cooldown lets a few cycles
    accumulate.

Output: `data/analysis_output/daemon_retrospective/daemon_retrospective.{json,md}`

CLI:
    python scripts/analysis/daemon_retrospective.py
        [--start-date YYYY-MM-DD] [--end-date YYYY-MM-DD]
        [--cooldown-days 14]
        [--event-log-path ...] [--stage2-brier-history-path ...]
        [--stage3-v2-drift-history-path ...]
        [--stake-scaling-report-path ...] [--walk-forward-cert-path ...]
        [--output-dir ...]
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple


PROJECT_DIR = Path(__file__).resolve().parents[2]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from scripts.analysis import promote  # noqa: E402
from scripts.analysis import auto_promote_demote_daemon as daemon  # noqa: E402


DEFAULT_OUTPUT_DIR = PROJECT_DIR / "data" / "analysis_output" / "daemon_retrospective"
DEFAULT_READY_MIN_DATES = 7


AGREEMENT_MATCH = "MATCH"
AGREEMENT_DAEMON_ONLY = "DAEMON_ONLY"
AGREEMENT_OPERATOR_ONLY = "OPERATOR_ONLY"
AGREEMENT_DAEMON_DISAGREED = "DAEMON_DISAGREED"
AGREEMENT_BOTH_NO_ACTION = "BOTH_NO_ACTION"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _date_of_history_row(row: Dict[str, Any]) -> Optional[str]:
    """Most history rows carry both `data_max_date` (preferred -- the
    date the data covered) and `generated_at_utc` (when the row was
    written). We use `data_max_date` for the replay -- it's what the
    daemon would have been evaluating against on that day."""
    d = row.get("data_max_date")
    if isinstance(d, str) and len(d) >= 10:
        return d[:10]
    gen = row.get("generated_at_utc")
    if isinstance(gen, str) and len(gen) >= 10:
        return gen[:10]
    return None


def distinct_history_dates(rows: List[Dict[str, Any]]) -> List[str]:
    """Return sorted unique dates from history rows."""
    seen: set = set()
    for r in rows:
        d = _date_of_history_row(r)
        if d:
            seen.add(d)
    return sorted(seen)


def slice_history_le(
    rows: List[Dict[str, Any]], max_date: str,
) -> List[Dict[str, Any]]:
    """Return rows with date <= max_date. Used to reconstruct the
    daemon's view of history AS OF max_date."""
    out: List[Dict[str, Any]] = []
    for r in rows:
        d = _date_of_history_row(r)
        if d and d <= max_date:
            out.append(r)
    return out


def events_le(
    events: List[Dict[str, Any]], max_date: str,
) -> List[Dict[str, Any]]:
    """Return events with generated_at_utc date prefix <= max_date.
    Used to reconstruct the daemon's cooldown evaluation AS OF
    max_date."""
    out: List[Dict[str, Any]] = []
    for e in events:
        gen = e.get("generated_at_utc")
        if isinstance(gen, str) and gen[:10] <= max_date:
            out.append(e)
    return out


def events_lt(
    events: List[Dict[str, Any]], max_date: str,
) -> List[Dict[str, Any]]:
    """Return events with generated_at_utc date STRICTLY BEFORE max_date.
    Used for cooldown evaluation: the daemon runs as part of the
    morning refresh, so on day D it sees events through D-1. Without
    this, replaying a day on which the operator acted always falsely
    looks like a cooldown skip (the operator's same-day event blocks
    the daemon's eval)."""
    out: List[Dict[str, Any]] = []
    for e in events:
        gen = e.get("generated_at_utc")
        if isinstance(gen, str) and gen[:10] < max_date:
            out.append(e)
    return out


def operator_action_on_date(
    events: List[Dict[str, Any]], lever_underscore: str, date: str,
) -> Optional[Dict[str, Any]]:
    """Return the operator's action on `date` for `lever`. Filters to
    events with success actions (promoted/forced/demoted). Returns the
    most recent on that date, or None if none.

    `lever_underscore` is the audit-log form (e.g. "stage2",
    "stage3_v2"). Caller does the hyphen->underscore conversion.
    """
    matches = [
        e for e in events
        if str(e.get("lever") or "") == lever_underscore
        and isinstance(e.get("generated_at_utc"), str)
        and e["generated_at_utc"][:10] == date
        and str(e.get("action") or "") in ("promoted", "forced", "demoted")
    ]
    if not matches:
        return None
    # Multiple ops in one day -> pick the latest by timestamp
    return max(matches, key=lambda e: str(e.get("generated_at_utc") or ""))


# ---------------------------------------------------------------------------
# Per-lever replay
# ---------------------------------------------------------------------------


def _stage2_verdict_for_history(history: List[Dict[str, Any]]) -> str:
    """Return verdict label for a (possibly sliced) stage2 history."""
    v = promote._stage2_promotion_verdict(history)
    return str(v.get("verdict") or "")


def _stage3_v2_verdict_for_history(history: List[Dict[str, Any]]) -> str:
    v = promote._stage3_v2_promotion_verdict(history)
    return str(v.get("verdict") or "")


# Each lever knows: how to load history, how to compute verdict from
# sliced history, and what its underscore name is in the audit log.
LEVER_REPLAY_CONFIG = {
    "stage2": {
        "load_history": promote._load_stage2_brier_history,
        "verdict_fn": _stage2_verdict_for_history,
        "audit_lever_name": "stage2",
    },
    "stage3-v2": {
        "load_history": promote._load_stage3_v2_drift_history,
        "verdict_fn": _stage3_v2_verdict_for_history,
        "audit_lever_name": "stage3_v2",
    },
}


def replay_lever_for_date(
    *,
    lever: str,
    history: List[Dict[str, Any]],
    events: List[Dict[str, Any]],
    date: str,
    cooldown_days: int,
) -> Dict[str, Any]:
    """Reconstruct what the daemon WOULD have decided on `date` for
    one lever. Returns the same shape `daemon.evaluate_lever` does
    (decision / direction / verdict_label / reason).

    Note: this only reproduces the promote-path. Demote-path replay
    is out-of-scope for v1 (the verdict needs session windows that
    only mature post-promotion).
    """
    cfg = LEVER_REPLAY_CONFIG[lever]
    sliced_history = slice_history_le(history, date)
    # Cooldown evaluation uses STRICT-less-than: the daemon runs at
    # refresh time and only sees prior-day events. Same-day operator
    # actions don't appear in cooldown evidence.
    sliced_events = events_lt(events, date)

    decision: Dict[str, Any] = {
        "lever": lever,
        "date": date,
        "decision": "no_action",
        "direction": None,
        "verdict_label": None,
        "reason": "",
    }
    verdict_label = cfg["verdict_fn"](sliced_history)
    decision["verdict_label"] = verdict_label

    if verdict_label != "promote":
        decision["reason"] = f"verdict={verdict_label}"
        return decision

    ok, cd_reason = daemon._cooldown_ok(
        lever_key=lever, events=sliced_events,
        today=date, cooldown_days=cooldown_days,
    )
    if not ok:
        decision["decision"] = "skipped_cooldown"
        decision["direction"] = "promote"
        decision["reason"] = cd_reason or ""
        return decision

    decision["decision"] = "would_promote"
    decision["direction"] = "promote"
    decision["reason"] = "promote verdict + cooldown ok"
    return decision


# ---------------------------------------------------------------------------
# Agreement classification
# ---------------------------------------------------------------------------


def classify_agreement(
    daemon_decision: Dict[str, Any], operator_action: Optional[Dict[str, Any]],
) -> str:
    """Bucket a (daemon, operator) pair into one of the AGREEMENT_*
    categories."""
    daemon_acted = daemon_decision.get("decision") in (
        "would_promote", "would_demote", "promoting", "demoting",
        "auto_promoted", "auto_demoted",
        # Active #13 fast Wilson-UB demote labels (2026-05-17)
        "would_demote_fast", "demoting_fast",
    )
    daemon_direction = daemon_decision.get("direction")

    if operator_action is None:
        if daemon_acted:
            return AGREEMENT_DAEMON_ONLY
        return AGREEMENT_BOTH_NO_ACTION

    # operator acted
    op_direction = "demote" if (
        operator_action.get("action") == "demoted"
        or str(operator_action.get("direction") or "promote") == "demote"
    ) else "promote"

    if not daemon_acted:
        return AGREEMENT_OPERATOR_ONLY

    if daemon_direction == op_direction:
        return AGREEMENT_MATCH
    return AGREEMENT_DAEMON_DISAGREED


# ---------------------------------------------------------------------------
# Per-lever replay over a date range
# ---------------------------------------------------------------------------


def _readiness_verdict(summary: Dict[str, int], *, min_dates: int) -> str:
    """Synthesise the per-lever 'ready_for_act' / 'needs_more_history' /
    'disagreements_present' label."""
    total = (
        summary["match_count"]
        + summary["daemon_only_count"]
        + summary["operator_only_count"]
        + summary["daemon_disagreed_count"]
        + summary["both_no_action_count"]
    )
    if total < min_dates:
        return "needs_more_history"
    if summary["daemon_disagreed_count"] > 0 or summary["daemon_only_count"] > 0:
        return "disagreements_present"
    return "ready_for_act"


def replay_lever(
    *,
    lever: str,
    history: List[Dict[str, Any]],
    events: List[Dict[str, Any]],
    start_date: Optional[str],
    end_date: Optional[str],
    cooldown_days: int,
    ready_min_dates: int = DEFAULT_READY_MIN_DATES,
) -> Dict[str, Any]:
    """Run the per-date replay across all dates in [start_date, end_date]
    (or every available date if both are None). Returns the per-date
    decisions, the per-category counts, and a readiness verdict.
    """
    cfg = LEVER_REPLAY_CONFIG[lever]
    audit_name = cfg["audit_lever_name"]
    all_dates = distinct_history_dates(history)
    if start_date:
        all_dates = [d for d in all_dates if d >= start_date]
    if end_date:
        all_dates = [d for d in all_dates if d <= end_date]

    per_date: List[Dict[str, Any]] = []
    counts = {
        "match_count": 0,
        "daemon_only_count": 0,
        "operator_only_count": 0,
        "daemon_disagreed_count": 0,
        "both_no_action_count": 0,
    }
    last_disagree_date: Optional[str] = None

    for d in all_dates:
        daemon_decision = replay_lever_for_date(
            lever=lever, history=history, events=events,
            date=d, cooldown_days=cooldown_days,
        )
        op_action = operator_action_on_date(events, audit_name, d)
        agreement = classify_agreement(daemon_decision, op_action)
        if agreement == AGREEMENT_MATCH:
            counts["match_count"] += 1
        elif agreement == AGREEMENT_DAEMON_ONLY:
            counts["daemon_only_count"] += 1
            last_disagree_date = d
        elif agreement == AGREEMENT_OPERATOR_ONLY:
            counts["operator_only_count"] += 1
        elif agreement == AGREEMENT_DAEMON_DISAGREED:
            counts["daemon_disagreed_count"] += 1
            last_disagree_date = d
        else:
            counts["both_no_action_count"] += 1
        per_date.append({
            "date": d,
            "daemon_decision": daemon_decision["decision"],
            "daemon_direction": daemon_decision["direction"],
            "daemon_verdict_label": daemon_decision["verdict_label"],
            "operator_action": (
                None if op_action is None
                else {
                    "action": op_action.get("action"),
                    "direction": op_action.get("direction", "promote"),
                    "operator": op_action.get("operator"),
                }
            ),
            "agreement": agreement,
        })

    summary = {**counts}
    summary["n_dates_evaluated"] = len(all_dates)
    summary["last_disagreement_date"] = last_disagree_date
    summary["readiness_for_act"] = _readiness_verdict(
        counts, min_dates=ready_min_dates,
    )
    return {
        "lever": lever,
        "audit_lever_name": audit_name,
        "date_range": {
            "start": all_dates[0] if all_dates else None,
            "end": all_dates[-1] if all_dates else None,
        },
        "per_date": per_date,
        "summary": summary,
    }


# ---------------------------------------------------------------------------
# Snapshots for non-time-series levers (stake-scaling, gate-threshold)
# ---------------------------------------------------------------------------


def snapshot_stake_scaling(stake_scaling_report_path: Path) -> Dict[str, Any]:
    payload, err = promote._safe_load_json(stake_scaling_report_path)
    if err or not isinstance(payload, dict):
        return {
            "lever": "stake-scaling",
            "verdict_label": "unreadable",
            "error": str(err),
            "actuated_by_daemon": True,  # post-2026-05-16 overrides file
        }
    verdict = str(payload.get("verdict") or "")
    return {
        "lever": "stake-scaling",
        "verdict_label": verdict,
        "n_sessions": payload.get("n_sessions"),
        "min_sessions": (payload.get("thresholds") or {}).get("min_sessions"),
        "would_promote_today": verdict == "promote",
        "actuated_by_daemon": True,
        "note": (
            "Snapshot only (no per-date history file). "
            "As of 2026-05-16, daemon auto-actuates this lever in act mode "
            "via the runtime-overrides file."
        ),
    }


def snapshot_gate_threshold(walk_forward_cert_path: Path) -> Dict[str, Any]:
    payload, err = promote._safe_load_json(walk_forward_cert_path)
    if err or not isinstance(payload, dict):
        return {
            "lever": "gate-threshold",
            "verdict_label": "unreadable",
            "error": str(err),
            "actuated_by_daemon": False,
        }
    actionable: List[Dict[str, Any]] = []
    for entry in payload.get("gates") or []:
        v = entry.get("verdict") or {}
        if str(v.get("verdict") or "").upper() in ("RETUNE", "RETIRE"):
            actionable.append({
                "name": entry.get("name"),
                "verdict": v.get("verdict"),
                "current_threshold": entry.get("current_threshold"),
                "recommended_threshold": v.get("recommended_threshold"),
            })
    return {
        "lever": "gate-threshold",
        "verdict_label": "promote" if actionable else "hold",
        "readiness": (payload.get("readiness") or {}).get("label"),
        "actionable_gates": actionable,
        "actuated_by_daemon": False,
        "note": (
            "Snapshot only. Daemon does NOT auto-actuate gate-threshold "
            "(value selection is operator-in-loop even with the overrides "
            "file). Operator runs `promote.py gate-threshold <gate> <value>`."
        ),
    }


# ---------------------------------------------------------------------------
# Report assembly
# ---------------------------------------------------------------------------


def build_report(
    *,
    stage2_history: List[Dict[str, Any]],
    stage3_v2_history: List[Dict[str, Any]],
    events: List[Dict[str, Any]],
    stake_scaling_report_path: Path,
    walk_forward_cert_path: Path,
    start_date: Optional[str],
    end_date: Optional[str],
    cooldown_days: int,
    ready_min_dates: int = DEFAULT_READY_MIN_DATES,
) -> Dict[str, Any]:
    replays = {
        "stage2": replay_lever(
            lever="stage2", history=stage2_history, events=events,
            start_date=start_date, end_date=end_date,
            cooldown_days=cooldown_days, ready_min_dates=ready_min_dates,
        ),
        "stage3-v2": replay_lever(
            lever="stage3-v2", history=stage3_v2_history, events=events,
            start_date=start_date, end_date=end_date,
            cooldown_days=cooldown_days, ready_min_dates=ready_min_dates,
        ),
    }
    snapshots = {
        "stake-scaling": snapshot_stake_scaling(stake_scaling_report_path),
        "gate-threshold": snapshot_gate_threshold(walk_forward_cert_path),
    }

    overall_ready = all(
        r["summary"]["readiness_for_act"] == "ready_for_act"
        for r in replays.values()
    )

    return {
        "generated_at_utc": _now_iso(),
        "config": {
            "start_date": start_date,
            "end_date": end_date,
            "cooldown_days": cooldown_days,
            "ready_min_dates": ready_min_dates,
        },
        "replays": replays,
        "snapshots": snapshots,
        "overall": {
            "ready_for_act_all_levers": overall_ready,
            "note": (
                "ready_for_act_all_levers reflects ONLY the time-series "
                "levers (stage2, stage3-v2). stake-scaling actuates via "
                "the overrides file with no historical replay; "
                "gate-threshold is preview-only by design."
            ),
        },
    }


# ---------------------------------------------------------------------------
# Markdown rendering
# ---------------------------------------------------------------------------


def render_markdown(report: Dict[str, Any]) -> str:
    lines: List[str] = []
    lines.append("# Daemon Retrospective Report")
    lines.append("")
    lines.append(f"_Generated: {report['generated_at_utc']}_")
    cfg = report["config"]
    lines.append(
        f"_Config: start={cfg['start_date']}, end={cfg['end_date']}, "
        f"cooldown_days={cfg['cooldown_days']}, "
        f"ready_min_dates={cfg['ready_min_dates']}_"
    )
    lines.append("")
    lines.append(
        f"**Overall ready_for_act (time-series levers only):** "
        f"`{report['overall']['ready_for_act_all_levers']}`"
    )
    lines.append("")
    lines.append("## Replayed levers (per-date)")
    lines.append("")
    for lever_name in ("stage2", "stage3-v2"):
        r = report["replays"][lever_name]
        s = r["summary"]
        lines.append(f"### {lever_name}")
        lines.append("")
        lines.append(f"- **Readiness:** `{s['readiness_for_act']}`")
        lines.append(
            f"- **Dates evaluated:** {s['n_dates_evaluated']} "
            f"(range: {r['date_range']['start']} -> {r['date_range']['end']})"
        )
        lines.append(
            f"- **MATCH:** {s['match_count']}  "
            f"**DAEMON_ONLY:** {s['daemon_only_count']}  "
            f"**OPERATOR_ONLY:** {s['operator_only_count']}  "
            f"**DAEMON_DISAGREED:** {s['daemon_disagreed_count']}  "
            f"**BOTH_NO_ACTION:** {s['both_no_action_count']}"
        )
        if s["last_disagreement_date"]:
            lines.append(
                f"- **Most recent disagreement:** {s['last_disagreement_date']}"
            )
        lines.append("")
        # Per-date table (only show non-BOTH_NO_ACTION rows to keep it readable)
        interesting = [
            d for d in r["per_date"]
            if d["agreement"] != AGREEMENT_BOTH_NO_ACTION
        ]
        if interesting:
            lines.append("| date | daemon decision | operator action | agreement |")
            lines.append("|---|---|---|---|")
            for d in interesting:
                op = d.get("operator_action")
                op_text = (
                    "(none)" if op is None
                    else f"{op['action']} ({op['direction']}) by {op['operator']}"
                )
                lines.append(
                    f"| {d['date']} "
                    f"| {d['daemon_decision']} "
                    f"({d.get('daemon_direction') or '-'}, "
                    f"verdict={d.get('daemon_verdict_label')}) "
                    f"| {op_text} | **{d['agreement']}** |"
                )
            lines.append("")
        else:
            lines.append("_(All days were `BOTH_NO_ACTION` -- nothing to show.)_")
            lines.append("")
    lines.append("## Snapshot levers (today only)")
    lines.append("")
    for lever_name in ("stake-scaling", "gate-threshold"):
        s = report["snapshots"][lever_name]
        lines.append(f"### {lever_name}")
        lines.append("")
        lines.append(f"- **verdict_label:** `{s.get('verdict_label')}`")
        lines.append(f"- **actuated_by_daemon:** {s.get('actuated_by_daemon')}")
        if s.get("note"):
            lines.append(f"- _{s['note']}_")
        if lever_name == "gate-threshold" and s.get("actionable_gates"):
            lines.append("")
            lines.append("Actionable gates today:")
            for g in s["actionable_gates"]:
                lines.append(
                    f"  - **{g['name']}**: {g['verdict']} "
                    f"(current={g['current_threshold']}, "
                    f"recommended={g['recommended_threshold']})"
                )
        lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI + main
# ---------------------------------------------------------------------------


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Daemon retrospective: replay decisions against history.",
    )
    p.add_argument("--start-date", type=str, default=None,
                   help="Earliest date to evaluate (YYYY-MM-DD). Default: all history.")
    p.add_argument("--end-date", type=str, default=None,
                   help="Latest date to evaluate (YYYY-MM-DD). Default: all history.")
    p.add_argument("--cooldown-days", type=int,
                   default=daemon.DEFAULT_AUTO_DAEMON_COOLDOWN_DAYS,
                   help="Cooldown window used in the replay (matches daemon default).")
    p.add_argument("--ready-min-dates", type=int, default=DEFAULT_READY_MIN_DATES,
                   help="Per-lever minimum dates evaluated before `ready_for_act`.")
    p.add_argument("--event-log-path", type=Path,
                   default=promote.DEFAULT_PROMOTION_EVENTS_LOG)
    p.add_argument("--stage2-brier-history-path", type=Path,
                   default=promote.DEFAULT_STAGE2_BRIER_HISTORY_PATH)
    p.add_argument("--stage3-v2-drift-history-path", type=Path,
                   default=promote.DEFAULT_STAGE3_V2_DRIFT_HISTORY_PATH)
    p.add_argument("--stake-scaling-report-path", type=Path,
                   default=promote.DEFAULT_STAKE_SCALING_REPORT_PATH)
    p.add_argument("--walk-forward-cert-path", type=Path,
                   default=promote.DEFAULT_WALK_FORWARD_CERT_PATH)
    p.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    return p.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    stage2_history = promote._load_stage2_brier_history(args.stage2_brier_history_path)
    stage3_v2_history = promote._load_stage3_v2_drift_history(args.stage3_v2_drift_history_path)
    events = promote.load_promotion_events(args.event_log_path)

    report = build_report(
        stage2_history=stage2_history,
        stage3_v2_history=stage3_v2_history,
        events=events,
        stake_scaling_report_path=args.stake_scaling_report_path,
        walk_forward_cert_path=args.walk_forward_cert_path,
        start_date=args.start_date,
        end_date=args.end_date,
        cooldown_days=args.cooldown_days,
        ready_min_dates=args.ready_min_dates,
    )

    args.output_dir.mkdir(parents=True, exist_ok=True)
    json_path = args.output_dir / "daemon_retrospective.json"
    md_path = args.output_dir / "daemon_retrospective.md"
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(render_markdown(report))

    overall = report["overall"]["ready_for_act_all_levers"]
    s2 = report["replays"]["stage2"]["summary"]
    s3 = report["replays"]["stage3-v2"]["summary"]
    print(
        f"daemon_retrospective: stage2 readiness={s2['readiness_for_act']} "
        f"(match={s2['match_count']} disagree={s2['daemon_disagreed_count']} "
        f"daemon_only={s2['daemon_only_count']}); "
        f"stage3-v2 readiness={s3['readiness_for_act']} "
        f"(match={s3['match_count']} disagree={s3['daemon_disagreed_count']} "
        f"daemon_only={s3['daemon_only_count']}); "
        f"overall_ready_for_act={overall}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
