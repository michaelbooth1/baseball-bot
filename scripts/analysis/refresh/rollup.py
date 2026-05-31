"""End-of-refresh operator summary (refresh_health_rollup step).

Reads the accumulated step results + latest daily review + walk-forward
summary + cross-artifact reports and produces one consolidated
"is the project healthy?" output. Descriptive only; never fails.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import List, Optional

from . import config as _config
from .config import (
    RefreshConfig,
    RefreshStep,
    RefreshStepResult,
)
from .execution import _output_tail


def _run_refresh_health_rollup(
    step: RefreshStep,
    config: RefreshConfig,
    prior_results: List[RefreshStepResult],
) -> RefreshStepResult:
    """Build the end-of-refresh operator summary from accumulated state.

    Reads: (a) the step results so far, (b) the latest daily human-review
    JSON if the daily_human_review step ran, (c) the walk-forward
    summary if walk_forward_score_event ran, (d) the model_freshness_health
    notes from earlier in this same refresh.

    Output is descriptive only and never fails the refresh.
    """
    started = time.monotonic()
    lines: List[str] = []
    try:
        steps_total = len(prior_results)
        steps_ok = sum(1 for r in prior_results if r.status == "ok")
        steps_failed = [r for r in prior_results if r.status == "failed"]
        lines.append(
            f"Step roll-up: {steps_ok}/{steps_total} ok"
            + (f", {len(steps_failed)} failed ({', '.join(r.name for r in steps_failed)})"
               if steps_failed else "")
        )

        # Pull alert counts from the latest daily human-review JSON.
        review_dir = _config.PROJECT_DIR / "data" / "analysis_output" / "daily_human_review"
        latest_review_path: Optional[Path] = None
        if review_dir.exists():
            review_files = sorted(review_dir.glob("*_human_review.json"))
            if review_files:
                latest_review_path = review_files[-1]
        if latest_review_path is not None:
            try:
                review = json.loads(latest_review_path.read_text(encoding="utf-8"))
            except Exception:
                review = {}
            review_date = review.get("session_date") or latest_review_path.stem.split("_")[0]
            alert_counts = {
                "calibration": len((review.get("calibration_health") or {}).get("alerts") or []),
                "fill_rate":   len((review.get("fill_rate_health") or {}).get("alerts") or []),
                "signal_qual": len((review.get("signal_quality_health") or {}).get("alerts") or []),
                "regime_mix":  len((review.get("regime_mix_health") or {}).get("alerts") or []),
                "reconciler":  len((review.get("reconciler_summary") or {}).get("alerts") or []),
            }
            total = sum(alert_counts.values())
            lines.append(
                f"Latest daily review ({review_date}): {total} active drift alerts "
                + "(" + ", ".join(f"{k}={v}" for k, v in alert_counts.items()) + ")"
            )
        else:
            lines.append("No daily human-review JSON found yet.")

        # Pull walk-forward summary if available.
        wf_summary_path = (
            _config.PROJECT_DIR / "data" / "analysis_output" / "walk_forward" / "summary.json"
        )
        if wf_summary_path.exists():
            try:
                wf = json.loads(wf_summary_path.read_text(encoding="utf-8"))
            except Exception:
                wf = {}
            base = wf.get("baseline_live_engine_results") or {}
            lines.append(
                "Walk-forward: "
                f"{wf.get('n_windows_completed', 0)}/{wf.get('n_windows_planned', 0)} windows, "
                f"baseline cumulative profit ${base.get('cumulative_baseline_realized_profit', 0):+.2f}, "
                f"max DD ${base.get('max_baseline_drawdown_across_test_windows', 0):+.2f}"
            )

        # Forward the model-freshness handler's tail if it ran.
        for r in prior_results:
            if r.name == "model_freshness_health" and r.output_tail:
                first_alerts = [
                    ln for ln in r.output_tail.splitlines()
                    if ln.strip().startswith(("ALERT", "WARNING"))
                ]
                if first_alerts:
                    lines.append("Model freshness alerts:")
                    lines.extend(f"  - {ln.strip()}" for ln in first_alerts[:10])
                else:
                    lines.append("Model freshness: no alerts.")
                break

        # Same treatment for the Stage-3 v2 promotion-readiness check.
        for r in prior_results:
            if r.name == "stage3_v2_promotion_check" and r.output_tail:
                first_alerts = [
                    ln for ln in r.output_tail.splitlines()
                    if ln.strip().startswith(("ALERT", "WARNING"))
                ]
                if first_alerts:
                    lines.append("Stage-3 v2 promotion alerts:")
                    lines.extend(f"  - {ln.strip()}" for ln in first_alerts[:5])
                else:
                    lines.append("Stage-3 v2 promotion: no alerts.")
                break

        # Auto-daemon decisions.
        for r in prior_results:
            if r.name == "auto_promote_demote_daemon" and r.output_tail:
                tail_lines = r.output_tail.splitlines()
                if tail_lines:
                    lines.append(f"Auto-daemon: {tail_lines[0]}")
                actionable = [
                    ln for ln in tail_lines
                    if ln.strip().startswith("ALERT")
                ]
                if actionable:
                    lines.extend(f"  - {ln.strip()}" for ln in actionable[:8])
                break

        # Stake-scaling promotion verdict.
        stake_scaling_path = (
            _config.PROJECT_DIR / "data" / "analysis_output"
            / "stake_scaling_analysis" / "stake_scaling_analysis.json"
        )
        if stake_scaling_path.exists():
            try:
                ss = json.loads(stake_scaling_path.read_text(encoding="utf-8"))
            except Exception:
                ss = {}
            verdict = str(ss.get("verdict") or "unknown")
            n_sessions = ss.get("n_sessions", 0)
            min_sessions = (ss.get("thresholds") or {}).get("min_sessions", 30)
            n_filled = ss.get("n_filled_bets", 0)
            prefix = "ALERT " if verdict == "promote" else ""
            lines.append(
                f"{prefix}Stake scaling: verdict={verdict} "
                f"({n_sessions}/{min_sessions} sessions, {n_filled} filled bets)"
            )
        else:
            lines.append("Stake scaling report not present (analyzer didn't run).")

        # Walk-forward certification scorecard.
        wfc_path = (
            _config.PROJECT_DIR / "data" / "analysis_output"
            / "walk_forward_certification" / "walk_forward_certification.json"
        )
        if wfc_path.exists():
            try:
                wfc = json.loads(wfc_path.read_text(encoding="utf-8"))
            except Exception:
                wfc = {}
            readiness = wfc.get("readiness") or {}
            label = str(readiness.get("label") or "unknown")
            gate_entries = wfc.get("gates") or []
            gate_counts = {"KEEP": 0, "RETUNE": 0, "RETIRE": 0}
            actionable: List[str] = []
            for entry in gate_entries:
                v = (entry.get("verdict") or {})
                vname = str(v.get("verdict") or "").upper()
                gate_counts[vname] = gate_counts.get(vname, 0) + 1
                if vname in ("RETUNE", "RETIRE"):
                    actionable.append(
                        f"  - {entry.get('name')} -> {vname}"
                        f" (recommended_threshold={v.get('recommended_threshold')}):"
                        f" {(v.get('reason') or '').strip()}"
                    )
            needs_action = gate_counts.get("RETUNE", 0) + gate_counts.get("RETIRE", 0) > 0
            prefix = "ALERT " if needs_action else ""
            lines.append(
                f"{prefix}Walk-forward certification: {label} "
                f"({readiness.get('n_filled', 0)} filled / {readiness.get('n_dates', 0)} dates); "
                f"gates: {gate_counts.get('KEEP', 0)} KEEP, "
                f"{gate_counts.get('RETUNE', 0)} RETUNE, "
                f"{gate_counts.get('RETIRE', 0)} RETIRE"
            )
            lines.extend(actionable[:10])
        else:
            lines.append("Walk-forward certification not present (builder didn't run).")

        lineage_path = (
            _config.PROJECT_DIR / "data" / "analysis_output"
            / "artifact_lineage_freshness" / "artifact_lineage_freshness_report.json"
        )
        if lineage_path.exists():
            try:
                lineage = json.loads(lineage_path.read_text(encoding="utf-8"))
            except Exception:
                lineage = {}
            summary = lineage.get("summary") or {}
            status = str(lineage.get("status") or "unknown")
            prefix = "ALERT " if status == "error" else ("WARNING " if status == "warning" else "")
            lines.append(
                f"{prefix}Artifact lineage freshness: status={status}, "
                f"{summary.get('ok', 0)} ok / {summary.get('warning', 0)} warning / "
                f"{summary.get('error', 0)} error, "
                f"stale_mtime={summary.get('stale_by_mtime', 0)}, "
                f"stale_max_date={summary.get('stale_by_max_date', 0)}"
            )
            actionable = [
                art for art in (lineage.get("artifacts") or [])
                if ((art.get("health") or {}).get("status") in {"warning", "error"})
            ]
            for art in actionable[:5]:
                health = art.get("health") or {}
                tags = list(health.get("errors") or []) + list(health.get("warnings") or [])
                lines.append(f"  - {art.get('name')}: {', '.join(tags[:4])}")
        else:
            lines.append("Artifact lineage freshness report not present (builder didn't run).")

        # Doc-freshness gauge (Phase 3b, 2026-05-25).
        try:
            from doc_freshness import render_summary_lines as _doc_freshness_lines  # type: ignore
        except Exception:
            try:
                from scripts.analysis.doc_freshness import (  # type: ignore
                    render_summary_lines as _doc_freshness_lines,
                )
            except Exception as exc:
                lines.append(f"Doc freshness check unavailable: {exc!r}")
                _doc_freshness_lines = None  # type: ignore
        if _doc_freshness_lines is not None:
            try:
                lines.extend(_doc_freshness_lines())
            except Exception as exc:
                lines.append(f"Doc freshness check raised (non-fatal): {exc!r}")
    except Exception as exc:
        lines.append(f"Health rollup encountered an error (non-fatal): {exc!r}")

    elapsed = time.monotonic() - started
    return RefreshStepResult(
        name=step.name,
        command=[],
        returncode=0,
        elapsed_secs=round(elapsed, 3),
        status="ok",
        output_tail=_output_tail("\n".join(lines)),
    )
