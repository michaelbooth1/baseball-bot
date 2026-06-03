"""`promote.py status` subcommand: read all verdicts + print one summary.

Calls into every lever's promotion verdict + windowed/fast demotion
verdicts, then prints a single block of operator-readable status.
"""
from __future__ import annotations

import argparse

from scripts.analysis.run_daily_refresh import (
    _load_stage2_brier_history,
    _load_stage3_v2_drift_history,
    _safe_load_json,
    _stage2_promotion_verdict,
    _stage3_v2_promotion_verdict,
)

from . import constants as _constants
from .demotion import (
    gate_threshold_demotion_verdict,
    stage2_demotion_verdict,
    stage3_v2_demotion_verdict,
    stake_scaling_demotion_verdict,
)
from .events import latest_promotion_event_for_lever, load_promotion_events
from .fast_demotion import (
    gate_threshold_fast_demote_verdict,
    stage2_fast_demote_verdict,
    stage3_v2_fast_demote_verdict,
    stake_scaling_fast_demote_verdict,
)
from .output import _print_block, _print_header
from .stage1_verdict import (
    _stage1_promotion_verdict,
    stage1_demotion_verdict,
    stage1_fast_demote_verdict,
)


def cmd_status(args: argparse.Namespace) -> int:
    _print_header("Promotion verdict status")

    # Stage-1 (2026-05-17). Defensive getattr keeps existing tests
    # that pass a minimal SimpleNamespace working; the real CLI
    # parser sets these defaults.
    s1_source_path = getattr(
        args, "stage1_source_path", _constants.DEFAULT_STAGE1_STAGING_PATH,
    )
    s1_cache_path = getattr(
        args, "stage1_cache_path", _constants.DEFAULT_STAGE1_CACHE_PATH,
    )
    s1_verdict = _stage1_promotion_verdict(
        source_path=s1_source_path,
        production_path=s1_cache_path,
    )
    print()
    print(f"[stage1] verdict: {s1_verdict['verdict']}")
    _print_block("source path", s1_source_path.name)
    _print_block(
        "source built_at_utc",
        s1_verdict.get("source_built_at_utc") or "<not present>",
    )
    _print_block(
        "production built_at_utc",
        s1_verdict.get("production_built_at_utc") or "<not present>",
    )
    if s1_verdict.get("verdict_reason"):
        _print_block("reason", s1_verdict["verdict_reason"])

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
    sessions_dir = getattr(args, "sessions_dir", _constants.DEFAULT_SESSIONS_DIR)
    events = load_promotion_events(args.event_log_path)
    demotion_verdicts = {
        "stage1": stage1_demotion_verdict(events=events, sessions_dir=sessions_dir),
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
        "stage1": stage1_fast_demote_verdict(events=events, sessions_dir=sessions_dir),
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
