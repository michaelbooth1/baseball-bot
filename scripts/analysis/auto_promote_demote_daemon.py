#!/usr/bin/env python3
"""auto_promote_demote_daemon.py -- automated promotion / demotion based
on the daily refresh's stability-gate verdicts.

This is the transition from "self-improving with human approver" to
"self-improving with human reviewer". The promote.py CLI gave the
operator a one-command path for each lever; this daemon takes the next
step by reading the same verdicts and (when conditions are met)
invoking promote.py automatically with operator="auto_daemon".

Safety model
------------
1. **Preview-by-default**. Ships in `--mode preview` mode: logs what it
   would do, does nothing. Operator opts into `--mode act` after
   reviewing preview output. Killable entirely with `--mode off`.

2. **Cooldown lock-in**. Refuses to auto-act on a lever that had ANY
   action (promote/demote, manual or daemon) in the last `--cooldown-days`
   days (default 14, matching the demotion verdict's pre/post window).
   Protects manual operator decisions from immediate override AND lets
   the demotion verdict gather evidence before the daemon can swing back.

3. **Trust the existing stability gates**. We do not stack another
   N-consecutive-refresh check on top of the verdicts. The verdicts
   already encode multi-day stability (5/7 day rule for Stage-2 and
   Stage-3 v2; 30-session rule for stake-scaling). The daemon trusts
   them and adds only the cooldown.

4. **Auto-actuated levers**. v1 auto-acts on `stage2`, `stage3-v2`
   (file-swap), AND `stake-scaling` (binary state via the runtime-
   overrides config layer that shipped 2026-05-16). The fourth lever
   `gate-threshold` remains preview-only because the *threshold value*
   to choose (from the walk-forward certification's `RETUNE`
   recommendation) is a per-gate judgment that benefits from operator
   review even when an override-file mechanism exists. The daemon
   surfaces the verdict so the operator can run
   `promote.py gate-threshold <gate> <value>` themselves.

5. **Subprocess isolation**. Each promote.py invocation runs in a
   subprocess; a failed action doesn't crash the daemon, doesn't crash
   the refresh, and gets logged distinctly.

Audit trail
-----------
Daemon actions write to the standard `promotion_events.jsonl` (via
promote.py's audit log) with `operator="auto_daemon"`. Skip decisions
(cooldown, no-go verdict, opt-out) are logged to stdout only -- they
go into the refresh manifest's `output_tail` and the
`refresh_health_rollup` summary, but they don't pollute the
actual-action log.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


PROJECT_DIR = Path(__file__).resolve().parents[2]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

# Reuse the verdict primitives + audit-log readers from promote.py.
# No duplication; one source of truth for "what does each verdict mean".
from scripts.analysis import promote  # noqa: E402

DEFAULT_PROMOTE_SCRIPT = PROJECT_DIR / "scripts" / "analysis" / "promote.py"
DEFAULT_AUTO_DAEMON_COOLDOWN_DAYS = 14
DEFAULT_AUTO_DAEMON_OPERATOR_LABEL = "auto_daemon"

MODES = ("preview", "act", "off")

# Auto-actuatable levers. Order is deliberate: stage2 first (smaller-
# effect, more-tested), stage3-v2 second, stake-scaling third (newer
# actuation path through the live-overrides file, binary state so
# value selection is safe to automate). `gate-threshold` is preview-
# only -- the *threshold value* to pick is a per-gate judgment call,
# not a binary go/no-go, so we surface the verdict but let the
# operator run `promote.py gate-threshold <gate> <value>` themselves.
AUTO_ACT_LEVERS: Tuple[str, ...] = ("stage2", "stage3-v2", "stake-scaling")
PREVIEW_ONLY_LEVERS: Tuple[str, ...] = ("gate-threshold",)
# Back-compat alias for tests / external callers that import the
# original constant names. CLI_FLAG_LEVERS is the historical name for
# levers the daemon couldn't actuate; we keep it as an alias for
# `gate-threshold` only (the one CLI-flag lever still preview-only).
FILE_SWAP_LEVERS: Tuple[str, ...] = ("stage2", "stage3-v2")
CLI_FLAG_LEVERS: Tuple[str, ...] = PREVIEW_ONLY_LEVERS
ALL_LEVERS: Tuple[str, ...] = AUTO_ACT_LEVERS + PREVIEW_ONLY_LEVERS


# ---------------------------------------------------------------------------
# Verdict adapters: convert each lever's status into a daemon-friendly
# decision tuple (action, verdict_label, details).
# ---------------------------------------------------------------------------


def _stage2_promote_verdict_label(args: argparse.Namespace) -> Tuple[str, Dict[str, Any]]:
    history = promote._load_stage2_brier_history(args.stage2_brier_history_path)
    verdict = promote._stage2_promotion_verdict(history)
    return str(verdict.get("verdict") or ""), verdict


def _stage3_v2_promote_verdict_label(args: argparse.Namespace) -> Tuple[str, Dict[str, Any]]:
    history = promote._load_stage3_v2_drift_history(args.stage3_v2_drift_history_path)
    verdict = promote._stage3_v2_promotion_verdict(history)
    return str(verdict.get("verdict") or ""), verdict


def _stake_scaling_promote_verdict_label(args: argparse.Namespace) -> Tuple[str, Dict[str, Any]]:
    payload, err = promote._safe_load_json(args.stake_scaling_report_path)
    if err or not isinstance(payload, dict):
        return "unreadable", {"error": str(err)}
    return str(payload.get("verdict") or ""), payload


def _gate_threshold_promote_verdict_label(
    args: argparse.Namespace,
) -> Tuple[str, Dict[str, Any]]:
    """Walk-forward certification doesn't have a single overall verdict --
    it has per-gate KEEP/RETUNE/RETIRE. For the daemon's purposes we
    summarise as 'promote' iff any gate reads RETUNE/RETIRE; otherwise
    'hold'. Since gate-threshold is CLI-flag (not actuated by the
    daemon) the label is informational only.
    """
    payload, err = promote._safe_load_json(args.walk_forward_cert_path)
    if err or not isinstance(payload, dict):
        return "unreadable", {"error": str(err)}
    actionable: List[str] = []
    for entry in payload.get("gates") or []:
        v = (entry.get("verdict") or {})
        if str(v.get("verdict") or "").upper() in ("RETUNE", "RETIRE"):
            actionable.append(str(entry.get("name") or "?"))
    label = "promote" if actionable else "hold"
    return label, {"actionable_gates": actionable, "readiness": (payload.get("readiness") or {}).get("label")}


def _stage2_demote_verdict_label(
    args: argparse.Namespace, events: List[Dict[str, Any]],
) -> Tuple[str, Dict[str, Any]]:
    v = promote.stage2_demotion_verdict(events=events, sessions_dir=args.sessions_dir)
    return str(v.get("verdict") or ""), v


def _stage3_v2_demote_verdict_label(
    args: argparse.Namespace, events: List[Dict[str, Any]],
) -> Tuple[str, Dict[str, Any]]:
    v = promote.stage3_v2_demotion_verdict(events=events, sessions_dir=args.sessions_dir)
    return str(v.get("verdict") or ""), v


def _stake_scaling_demote_verdict_label(
    args: argparse.Namespace, events: List[Dict[str, Any]],
) -> Tuple[str, Dict[str, Any]]:
    v = promote.stake_scaling_demotion_verdict(events=events, sessions_dir=args.sessions_dir)
    return str(v.get("verdict") or ""), v


def _gate_threshold_demote_verdict_label(
    args: argparse.Namespace, events: List[Dict[str, Any]],
) -> Tuple[str, Dict[str, Any]]:
    v = promote.gate_threshold_demotion_verdict(events=events, sessions_dir=args.sessions_dir)
    return str(v.get("verdict") or ""), v


# Active #13 (2026-05-17): fast Wilson-UB demote verdicts. Parallel
# to the windowed verdicts above. The daemon evaluates BOTH and
# prefers fast_demote when it fires -- it carries 95% one-sided
# confidence on win rate vs breakeven from N>=20 post-promotion
# bets, which is a stronger statistical signal than the windowed
# 10pp-ROI-gap check. Critically, fast_demote BYPASSES the standard
# cooldown: the whole point is to react in 5-6 days, not 14+.

def _stage2_fast_demote_verdict_label(
    args: argparse.Namespace, events: List[Dict[str, Any]],
) -> Tuple[str, Dict[str, Any]]:
    v = promote.stage2_fast_demote_verdict(events=events, sessions_dir=args.sessions_dir)
    return str(v.get("verdict") or ""), v


def _stage3_v2_fast_demote_verdict_label(
    args: argparse.Namespace, events: List[Dict[str, Any]],
) -> Tuple[str, Dict[str, Any]]:
    v = promote.stage3_v2_fast_demote_verdict(events=events, sessions_dir=args.sessions_dir)
    return str(v.get("verdict") or ""), v


def _stake_scaling_fast_demote_verdict_label(
    args: argparse.Namespace, events: List[Dict[str, Any]],
) -> Tuple[str, Dict[str, Any]]:
    v = promote.stake_scaling_fast_demote_verdict(events=events, sessions_dir=args.sessions_dir)
    return str(v.get("verdict") or ""), v


def _gate_threshold_fast_demote_verdict_label(
    args: argparse.Namespace, events: List[Dict[str, Any]],
) -> Tuple[str, Dict[str, Any]]:
    v = promote.gate_threshold_fast_demote_verdict(events=events, sessions_dir=args.sessions_dir)
    return str(v.get("verdict") or ""), v


# ---------------------------------------------------------------------------
# Cooldown check
# ---------------------------------------------------------------------------


def _last_action_for_lever(
    events: List[Dict[str, Any]], lever_key: str,
) -> Optional[Dict[str, Any]]:
    """Most recent ACTION event (any direction, any success action label)
    for `lever_key`. Used for the cooldown check. Maps the daemon's
    hyphenated lever names to the promote_events lever names (which use
    underscores: stage3-v2 -> stage3_v2)."""
    lever_in_log = lever_key.replace("-", "_")
    candidates = [
        r for r in events
        if str(r.get("lever") or "") == lever_in_log
        and str(r.get("action") or "") in ("promoted", "forced", "demoted")
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda r: str(r.get("generated_at_utc") or ""))


def _days_since_event(event: Dict[str, Any], *, today: str) -> Optional[float]:
    try:
        evt_dt = datetime.strptime(str(event.get("generated_at_utc") or "")[:10], "%Y-%m-%d")
        today_dt = datetime.strptime(today, "%Y-%m-%d")
        return (today_dt - evt_dt).total_seconds() / 86400.0
    except ValueError:
        return None


def _cooldown_ok(
    *, lever_key: str, events: List[Dict[str, Any]], today: str, cooldown_days: int,
) -> Tuple[bool, Optional[str]]:
    """Return (ok_to_act, reason). ok_to_act is False when ANY prior
    action (promote/demote, manual or daemon) happened within the
    cooldown window."""
    last = _last_action_for_lever(events, lever_key)
    if last is None:
        return True, None
    days = _days_since_event(last, today=today)
    if days is None:
        return True, None
    if days < cooldown_days:
        return False, (
            f"last action was {days:.1f}d ago "
            f"(operator={last.get('operator')}, action={last.get('action')}, "
            f"direction={last.get('direction', 'promote')}); "
            f"cooldown is {cooldown_days}d"
        )
    return True, None


# ---------------------------------------------------------------------------
# Per-lever decision logic
# ---------------------------------------------------------------------------


def _is_lever_opted_in(args: argparse.Namespace, lever: str, direction: str) -> bool:
    """Per-lever opt-outs: --no-auto-promote-stage2 / --no-auto-demote-stage2 etc.
    Defaults are True (opted in) unless explicitly disabled."""
    flag = f"no_auto_{direction}_{lever.replace('-', '_')}"
    return not bool(getattr(args, flag, False))


def evaluate_lever(
    *,
    lever: str,
    args: argparse.Namespace,
    events: List[Dict[str, Any]],
    today: str,
) -> Dict[str, Any]:
    """Decide whether to auto-act on one lever. Returns a structured
    decision dict that the daemon's main loop will either log (preview)
    or actuate (act mode)."""
    decision: Dict[str, Any] = {
        "lever": lever,
        "decision": "no_action",
        "direction": None,
        "verdict_label": None,
        "reason": "",
    }

    # Preview-only levers (gate-threshold): surface the verdict but
    # don't auto-act. The threshold *value* to choose isn't a binary
    # call so we keep operator-in-loop even with the overrides file
    # available.
    if lever in PREVIEW_ONLY_LEVERS:
        promote_label, _ = _PROMOTE_VERDICT_FNS[lever](args)
        decision.update(
            decision="skipped_preview_only_lever",
            direction="promote",
            verdict_label=promote_label,
            reason=(
                f"verdict={promote_label}; daemon does not auto-act on "
                f"{lever} (threshold value selection is operator-in-loop). "
                "Run `promote.py gate-threshold <gate> <value>` manually."
            ),
        )
        return decision

    # Auto-actuatable levers: evaluate promote first, then fast
    # demote, then windowed demote. Fast demote is checked AHEAD of
    # promote? No -- a `promote` verdict means today's data already
    # supports the promotion direction; a stale fast_demote against
    # an older promotion would be moot if today says promote. Order:
    #   1. promote verdict (advance the lever)
    #   2. fast_demote verdict (urgent rollback, bypasses cooldown)
    #   3. windowed demote verdict (cautious rollback, respects cooldown)
    promote_label, promote_details = _PROMOTE_VERDICT_FNS[lever](args)
    fast_demote_label, fast_demote_details = (
        _FAST_DEMOTE_VERDICT_FNS[lever](args, events)
    )
    demote_label, demote_details = _DEMOTE_VERDICT_FNS[lever](args, events)

    # If promote verdict says go AND lever opted in -> attempt promote.
    if promote_label == "promote":
        if not _is_lever_opted_in(args, lever, "promote"):
            decision.update(
                decision="skipped_opt_out",
                direction="promote",
                verdict_label=promote_label,
                reason=f"verdict=promote but --no-auto-promote-{lever} is set",
            )
            return decision
        ok, cd_reason = _cooldown_ok(
            lever_key=lever, events=events, today=today,
            cooldown_days=args.cooldown_days,
        )
        if not ok:
            decision.update(
                decision="skipped_cooldown",
                direction="promote",
                verdict_label=promote_label,
                reason=cd_reason,
            )
            return decision
        decision.update(
            decision="would_promote" if args.mode == "preview" else "promoting",
            direction="promote",
            verdict_label=promote_label,
            verdict_details=promote_details,
            reason="promote verdict + cooldown ok",
        )
        return decision

    # Fast demote (Active #13, 2026-05-17): bypass cooldown. The
    # standard cooldown exists to give the windowed demote check 14
    # days of post-promotion bets to gather evidence; the fast check
    # already has its evidence (N>=20 fills + Wilson UB <
    # breakeven at 95% confidence), so cooldown is irrelevant.
    # Bypass is the entire point -- without it, the fast check
    # could never fire on a recent promotion (cooldown would block).
    if fast_demote_label == "fast_demote":
        if not _is_lever_opted_in(args, lever, "demote"):
            decision.update(
                decision="skipped_opt_out",
                direction="demote",
                verdict_label=fast_demote_label,
                reason=(
                    f"fast_demote verdict but --no-auto-demote-{lever} "
                    "is set"
                ),
            )
            return decision
        decision.update(
            decision="would_demote_fast" if args.mode == "preview" else "demoting_fast",
            direction="demote",
            verdict_label=fast_demote_label,
            verdict_details=fast_demote_details,
            reason=(
                "fast_demote verdict: Wilson UB on post-promotion "
                "win rate is below breakeven at 95% confidence; "
                "cooldown bypassed."
            ),
            cooldown_bypassed=True,
        )
        return decision

    # If windowed demote verdict says go AND lever opted in -> attempt
    # demote (subject to the standard cooldown).
    if demote_label == "demote":
        if not _is_lever_opted_in(args, lever, "demote"):
            decision.update(
                decision="skipped_opt_out",
                direction="demote",
                verdict_label=demote_label,
                reason=f"verdict=demote but --no-auto-demote-{lever} is set",
            )
            return decision
        ok, cd_reason = _cooldown_ok(
            lever_key=lever, events=events, today=today,
            cooldown_days=args.cooldown_days,
        )
        if not ok:
            decision.update(
                decision="skipped_cooldown",
                direction="demote",
                verdict_label=demote_label,
                reason=cd_reason,
            )
            return decision
        decision.update(
            decision="would_demote" if args.mode == "preview" else "demoting",
            direction="demote",
            verdict_label=demote_label,
            verdict_details=demote_details,
            reason="demote verdict + cooldown ok",
        )
        return decision

    # Neither verdict actionable.
    decision.update(
        verdict_label=(
            f"promote={promote_label}, "
            f"fast_demote={fast_demote_label}, "
            f"demote={demote_label}"
        ),
        reason="no promote/fast_demote/demote verdict actionable",
    )
    return decision


# Adapters wired into the per-lever evaluator. Promote-verdict adapters
# take args; demote-verdict adapters take args + the loaded events log.
_PROMOTE_VERDICT_FNS = {
    "stage2": _stage2_promote_verdict_label,
    "stage3-v2": _stage3_v2_promote_verdict_label,
    "stake-scaling": _stake_scaling_promote_verdict_label,
    "gate-threshold": _gate_threshold_promote_verdict_label,
}
_DEMOTE_VERDICT_FNS = {
    "stage2": _stage2_demote_verdict_label,
    "stage3-v2": _stage3_v2_demote_verdict_label,
    "stake-scaling": _stake_scaling_demote_verdict_label,
    "gate-threshold": _gate_threshold_demote_verdict_label,
}
_FAST_DEMOTE_VERDICT_FNS = {
    "stage2": _stage2_fast_demote_verdict_label,
    "stage3-v2": _stage3_v2_fast_demote_verdict_label,
    "stake-scaling": _stake_scaling_fast_demote_verdict_label,
    "gate-threshold": _gate_threshold_fast_demote_verdict_label,
}


# ---------------------------------------------------------------------------
# Subprocess actuation
# ---------------------------------------------------------------------------


def _invoke_promote(
    *,
    lever: str,
    direction: str,
    args: argparse.Namespace,
) -> Dict[str, Any]:
    """Invoke promote.py for one lever in one direction. Returns a
    result dict capturing the subprocess outcome."""
    if direction == "promote":
        cmd = [sys.executable, str(args.promote_script), lever]
    else:
        cmd = [sys.executable, str(args.promote_script), "demote", lever]
    cmd.extend(["--operator", args.operator])
    proc = subprocess.run(
        cmd,
        cwd=str(PROJECT_DIR),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    return {
        "command": cmd,
        "returncode": proc.returncode,
        "stdout_tail": (proc.stdout or "").strip()[-1000:],
    }


def actuate(decision: Dict[str, Any], args: argparse.Namespace) -> Dict[str, Any]:
    """In `act` mode, run promote.py for a 'would_promote'/'would_demote'
    decision. Mutates the decision dict in place with subprocess result.
    Preview mode is a no-op: the decision already carries "would_*".
    """
    if args.mode != "act":
        return decision
    if decision["decision"] not in ("promoting", "demoting"):
        return decision
    lever = decision["lever"]
    direction = decision["direction"]
    result = _invoke_promote(lever=lever, direction=direction, args=args)
    decision["subprocess"] = result
    if result["returncode"] == 0:
        decision["decision"] = f"auto_{direction}d"  # auto_promoted | auto_demoted
    else:
        decision["decision"] = "failed"
        decision["reason"] = f"promote.py exited {result['returncode']}"
    return decision


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def render_summary(decisions: List[Dict[str, Any]], *, args: argparse.Namespace) -> str:
    lines: List[str] = []
    lines.append(f"auto_daemon mode={args.mode} cooldown_days={args.cooldown_days} active_date={args.active_date}")
    if args.mode == "off":
        lines.append("DAEMON DISABLED (mode=off); no verdicts read, no actions taken.")
        return "\n".join(lines)
    counts: Dict[str, int] = {}
    for d in decisions:
        counts[d["decision"]] = counts.get(d["decision"], 0) + 1
    lines.append("decisions: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    for d in decisions:
        prefix = (
            "ALERT" if d["decision"] in ("auto_promoted", "auto_demoted", "would_promote", "would_demote", "failed")
            else "ok   "
        )
        lines.append(
            f"  {prefix} [{d['lever']}] {d['decision']}"
            + (f"  (verdict={d.get('verdict_label')})" if d.get('verdict_label') else "")
            + (f"  -- {d['reason']}" if d.get("reason") else "")
        )
        sp = d.get("subprocess")
        if sp:
            rc = sp.get("returncode")
            tail = sp.get("stdout_tail") or ""
            for ln in tail.splitlines()[-3:]:
                lines.append(f"      | {ln}")
            lines.append(f"      | subprocess rc={rc}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI + main
# ---------------------------------------------------------------------------


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Auto promote/demote daemon (preview-by-default).",
    )
    p.add_argument("--mode", choices=MODES, default="preview",
                   help="preview (default; log decisions, take no action), act (invoke promote.py), off (skip entirely).")
    p.add_argument("--active-date", type=str, default=datetime.now().strftime("%Y-%m-%d"))
    p.add_argument("--cooldown-days", type=int, default=DEFAULT_AUTO_DAEMON_COOLDOWN_DAYS)
    p.add_argument("--operator", type=str, default=DEFAULT_AUTO_DAEMON_OPERATOR_LABEL,
                   help="Operator label written to promote_events.jsonl when daemon acts.")

    p.add_argument("--event-log-path", type=Path,
                   default=promote.DEFAULT_PROMOTION_EVENTS_LOG)
    p.add_argument("--sessions-dir", type=Path,
                   default=promote.DEFAULT_SESSIONS_DIR)
    p.add_argument("--stage2-brier-history-path", type=Path,
                   default=promote.DEFAULT_STAGE2_BRIER_HISTORY_PATH)
    p.add_argument("--stage3-v2-drift-history-path", type=Path,
                   default=promote.DEFAULT_STAGE3_V2_DRIFT_HISTORY_PATH)
    p.add_argument("--stake-scaling-report-path", type=Path,
                   default=promote.DEFAULT_STAKE_SCALING_REPORT_PATH)
    p.add_argument("--walk-forward-cert-path", type=Path,
                   default=promote.DEFAULT_WALK_FORWARD_CERT_PATH)
    p.add_argument("--promote-script", type=Path, default=DEFAULT_PROMOTE_SCRIPT)

    # Per-lever opt-outs (defaults: opted-IN for auto-actuatable levers)
    for lever in AUTO_ACT_LEVERS:
        slug = lever.replace("-", "_")
        p.add_argument(
            f"--no-auto-promote-{lever}", dest=f"no_auto_promote_{slug}",
            action="store_true",
            help=f"Skip auto-promotion on {lever} even when verdict says go.",
        )
        p.add_argument(
            f"--no-auto-demote-{lever}", dest=f"no_auto_demote_{slug}",
            action="store_true",
            help=f"Skip auto-demotion on {lever} even when verdict says go.",
        )

    return p.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    if args.mode == "off":
        print(render_summary([], args=args))
        return 0

    events = promote.load_promotion_events(args.event_log_path)
    decisions: List[Dict[str, Any]] = []
    for lever in ALL_LEVERS:
        decision = evaluate_lever(
            lever=lever, args=args, events=events, today=args.active_date,
        )
        decisions.append(actuate(decision, args))

    print(render_summary(decisions, args=args))
    # Daemon never fails the refresh: a subprocess failure on one lever
    # shouldn't take the whole refresh down. The "failed" decision shows
    # up in the summary, which the operator sees in refresh_health_rollup.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
