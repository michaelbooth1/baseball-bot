"""Markdown renderer for the daily human-review report.

Extracted from build_daily_human_review_report.py on 2026-05-25 as part
of the Tier-1 refactor to push the orchestrator file under 800 lines.
Pure render: takes a fully-built report dict and produces the .md
string. No I/O. The orchestrator file calls render_markdown(report)
to get the string, then write_report() persists it.

Public surface:
  - render_markdown(report: Dict[str, Any]) -> str
  - _markdown_table(headers, rows) -> List[str]  (helper, but
    re-exported for back-compat with any test that imported it)
"""
from __future__ import annotations

from typing import Any, Dict, List

from .helpers import _fmt_money, _fmt_pct, _safe_int


def _markdown_table(headers: List[str], rows: List[List[Any]]) -> List[str]:
    out = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    for row in rows:
        out.append("| " + " | ".join(str(cell) for cell in row) + " |")
    return out


def render_markdown(report: Dict[str, Any]) -> str:
    summary = report.get("session_summary") or {}
    bet_totals = report.get("bet_totals") or {}
    compact = report.get("candidate_rollup_compact") or {}
    log_counts = (report.get("log_health") or {}).get("counts") or {}

    lines: List[str] = [
        f"# MLB Polymarket Human Review - {report.get('session_date')}",
        "",
        "## Session",
        f"- Mode: {report.get('mode')}",
        f"- Bets: {_safe_int(summary.get('orders_placed'))} placed / {_safe_int(summary.get('orders_filled'))} filled",
        f"- Result: {_safe_int(summary.get('wins'))}-{_safe_int(summary.get('losses'))}, profit {_fmt_money(summary.get('total_profit'))}, ROI {_fmt_pct(summary.get('roi'))}",
        f"- Avg ask/limit/FV: {bet_totals.get('avg_entry_ask')} / {bet_totals.get('avg_limit_price')} / {bet_totals.get('avg_fair_value')}",
        "",
        "## Bets",
    ]

    bet_rows = []
    for bet in report.get("bets") or []:
        result = "W" if bet.get("won") is True else ("L" if bet.get("won") is False else "?")
        bet_rows.append([
            bet.get("game"),
            f"O{bet.get('line')}",
            bet.get("entry_ask"),
            bet.get("limit_price"),
            bet.get("actual_fill_price"),
            bet.get("fair_value"),
            bet.get("current_state_value_edge"),
            bet.get("phantom_risk_band"),
            result,
            _fmt_money(bet.get("profit")),
        ])
    lines.extend(_markdown_table(
        ["Game", "Line", "Ask", "Limit", "Fill", "FV", "Current Edge", "Phantom", "Result", "P&L"],
        bet_rows or [["none", "", "", "", "", "", "", "", "", ""]],
    ))

    lines.extend([
        "",
        "## Candidate Rollup",
        f"- Attempted rows: {_safe_int(compact.get('attempted_rows'))}",
        f"- Written rows: {_safe_int(compact.get('written_rows'))}",
        f"- Dedup suppressed: {_safe_int(compact.get('dedup_suppressed_rows'))}",
        f"- Write errors: {_safe_int(compact.get('write_error_rows'))}",
        "",
        "Top decision reasons:",
    ])
    for row in compact.get("top_decision_reasons") or []:
        lines.append(f"- {row['key']}: {row['count']}")

    lines.extend([
        "",
        "## Shadow Diagnostics",
        f"- EV shadow allow/block: {_safe_int(summary.get('ev_policy_shadow_allow'))}/{_safe_int(summary.get('ev_policy_shadow_block'))}",
        f"- Prob calibration shadow scored: {_safe_int(summary.get('prob_calibration_shadow_scored'))}",
        f"- No-score drift candidates: {_safe_int((compact.get('by_decision') or {}).get('shadow_no_score_drift'))}",
    ])
    feature_diag = report.get("shadow_feature_diagnostics") or {}
    feature_regimes = feature_diag.get("regimes") or {}
    for key in ("low_ask_high_edge", "runs_needed_exact_3p5", "home_skip_bottom9_risk"):
        row = feature_regimes.get(key) or {}
        if row:
            lines.append(
                f"- {key}: {_safe_int(row.get('placed'))} placed / "
                f"{_safe_int(row.get('filled'))} filled, P&L {_fmt_money(row.get('filled_profit'))}, "
                f"ROI {_fmt_pct(row.get('filled_roi'))}"
            )

    stage2_audit = report.get("stage2_suppression_dollar_audit") or {}
    lines.extend([
        "",
        "## Blocked Gate Dollar Audits",
        "- Stage-2 suppression: "
        f"{_safe_int(stage2_audit.get('labeled_rows'))} labeled blocked rows, "
        f"{_safe_int(stage2_audit.get('blocked_winning_rows'))} eventual winners / "
        f"{_safe_int(stage2_audit.get('blocked_losing_rows'))} eventual losers, "
        f"net hypothetical {_fmt_money(stage2_audit.get('net_hypothetical_profit_usdc'))}.",
    ])

    lines.extend([
        "",
        "## Log Health",
        f"- Schedule refresh lines: {_safe_int(log_counts.get('schedule_refreshed'))}",
        f"- Polling token-book lines: {_safe_int(log_counts.get('polling_token_books'))}",
        f"- Tick snapshot write lines: {_safe_int(log_counts.get('wrote_tick_snapshots'))}",
        f"- Warnings/errors: {_safe_int(log_counts.get('warnings'))}/{_safe_int(log_counts.get('errors'))}",
    ])

    cal = report.get("calibration_health") or {}
    cal_alerts = cal.get("alerts") or []
    artifact_methods = cal.get("artifact_methods_by_family") or {}
    sampled = cal.get("sampled_metrics_by_family") or {}
    method_changes = cal.get("method_changes_since_prior") or {}
    age = cal.get("artifact_age_days")
    lines.extend([
        "",
        "## Calibration Health",
        f"- Artifact present: {bool(cal.get('artifact_present'))}"
        + (f", age {age:.1f} days" if isinstance(age, (int, float)) else "")
        + f", default_family={cal.get('artifact_default_family')}",
        f"- Alerts: {len(cal_alerts)}",
    ])
    if artifact_methods:
        lines.append("- Artifact methods by family:")
        for family, method in sorted(artifact_methods.items()):
            lines.append(f"  - {family}: {method}")
    if sampled:
        rows = []
        for family, metrics in sorted(sampled.items()):
            rows.append([
                family,
                metrics.get("rows_with_both_probs", 0),
                metrics.get("mean_abs_delta"),
                metrics.get("max_abs_delta"),
                metrics.get("applied_share"),
            ])
        lines.append("- Sampled candidate-row deltas:")
        lines.extend(_markdown_table(
            ["Family", "N", "Mean |cal-raw|", "Max |cal-raw|", "Applied Share"],
            rows,
        ))
    if method_changes:
        lines.append("- Method changes vs prior daily review:")
        for family, change in method_changes.items():
            lines.append(f"  - {family}: {change.get('from')} -> {change.get('to')}")
    if cal_alerts:
        lines.append("- Active alerts:")
        for alert in cal_alerts:
            lines.append(f"  - {alert}")

    ce = report.get("calibrator_enforce_shipment_health") or {}
    ce_today = ce.get("today") or {}
    ce_effect = ce_today.get("enforce_effect") or {}
    ce_cal_metrics = ce_today.get("calibrator_metrics") or {}
    ce_baseline = ce.get("trailing_baseline") or {}
    ce_alerts = ce.get("alerts") or []
    lines.extend([
        "",
        "## Calibrator-Enforce Shipment (2026-05-19 patch)",
        f"- Decision mode: {ce.get('session_mode_at_decision_time')} "
        f"({ce.get('read_mode')}); status: {ce.get('status')}",
        f"- Candidates today: {ce_today.get('total_candidates_evaluated', 0)} "
        f"(trade={ce_today.get('trade_decisions', 0)}, "
        f"skip:gate_min_edge={ce_today.get('skip_due_to_gate_min_edge', 0)})",
        f"- In-band-gated (raw_fv>={ce.get('thresholds', {}).get('band_gate_threshold', 0.9):.2f}): "
        f"{ce_cal_metrics.get('in_band_gate_range_count', 0)}; "
        f"mean |cal-raw| in-band: "
        f"{_fmt_pct(ce_cal_metrics.get('mean_abs_delta_in_band'))}",
        f"- {ce_effect.get('attribution_label', 'effect')}: "
        f"{ce_effect.get('blocked_count', 0)}/"
        f"{ce_effect.get('candidate_pool_size', 0)} "
        f"({_fmt_pct(ce_effect.get('blocked_rate'))}) | by raw_fv: "
        f">=0.95={(ce_effect.get('blocked_by_raw_fv_bucket') or {}).get('>=0.95', 0)}, "
        f"0.90-0.95={(ce_effect.get('blocked_by_raw_fv_bucket') or {}).get('0.90-0.95', 0)}",
        (
            lambda bo, cf: (
                f"- Blocked outcomes: {bo.get('would_have_won', 0)}W / "
                f"{bo.get('would_have_lost', 0)}L of "
                f"{bo.get('settled_count', 0)} settled "
                f"({bo.get('undecided_count', 0)} undecided); "
                f"WR={_fmt_pct(bo.get('win_rate_among_settled'))}; "
                f"counterfactual save=${cf.get('saved_dollars', 0.0):+.2f} "
                f"@ default-stake ${cf.get('default_stake', 0.0):.0f} "
                f"[outcomes: {bo.get('outcomes_source_status', '?')}]"
            )
        )(
            ce_effect.get("blocked_outcomes") or {},
            (ce_effect.get("blocked_outcomes") or {}).get("counterfactual_pnl") or {},
        ),
        (
            lambda bb: (
                f"- Blocked outcomes by raw-FV band: "
                f"[0.90,0.95) {bb.get('0.90-0.95', {}).get('would_win', 0)}/"
                f"{bb.get('0.90-0.95', {}).get('settled', 0)} won "
                f"(save ${bb.get('0.90-0.95', {}).get('saved_dollars', 0.0):+.2f}); "
                f"[0.95,1.0) {bb.get('>=0.95', {}).get('would_win', 0)}/"
                f"{bb.get('>=0.95', {}).get('settled', 0)} won "
                f"(save ${bb.get('>=0.95', {}).get('saved_dollars', 0.0):+.2f})"
            )
        )((ce_effect.get("blocked_outcomes") or {}).get("by_raw_fv_band") or {}),
        f"- Trailing baseline: "
        f"today {ce_baseline.get('today_trades', 0)} trades vs "
        f"{ce_baseline.get('baseline_days_used', 0)}d mean "
        f"{ce_baseline.get('mean_daily_trades') or 'n/a'} "
        f"(ratio: {_fmt_pct(ce_baseline.get('today_volume_ratio_vs_baseline'))})",
    ])
    if ce_alerts:
        lines.append("- Active alerts:")
        for alert in ce_alerts:
            lines.append(f"  - {alert}")

    fill = report.get("fill_rate_health") or {}
    sig = report.get("signal_quality_health") or {}
    fill_today = fill.get("today") or {}
    fill_base = fill.get("baseline") or {}
    sig_today = sig.get("today") or {}
    sig_base = sig.get("baseline") or {}
    lines.extend([
        "",
        "## Drift Health",
        f"- Fill rate today: {fill_today.get('filled', 0)}/{fill_today.get('placed', 0)} "
        f"({_fmt_pct(fill_today.get('fill_rate'))}); trailing "
        f"{fill_base.get('days_in_baseline', 0)}d baseline: "
        f"{fill_base.get('filled', 0)}/{fill_base.get('placed', 0)} "
        f"({_fmt_pct(fill_base.get('fill_rate'))}).",
        f"- Filled win rate today: {sig_today.get('wins', 0)}/{sig_today.get('filled', 0)} "
        f"({_fmt_pct(sig_today.get('win_rate'))}); trailing "
        f"{sig_base.get('days_in_baseline', 0)}d baseline: "
        f"{sig_base.get('wins', 0)}/{sig_base.get('filled', 0)} "
        f"({_fmt_pct(sig_base.get('win_rate'))}).",
    ])
    regime = report.get("regime_mix_health") or {}
    tvds = regime.get("tvd_by_dimension") or {}
    if tvds:
        def _fmt_tvd(val: Any) -> str:
            return "n/a" if val is None else f"{float(val):.2f}"
        lines.append(
            f"- Regime-mix TVD vs trailing {regime.get('days_in_baseline', 0)}d "
            f"({regime.get('today_total_bets', 0)} bets today, "
            f"{regime.get('baseline_total_bets', 0)} baseline): "
            + ", ".join(
                f"{dim}={_fmt_tvd(val)}"
                for dim, val in sorted(tvds.items())
            )
            + "."
        )
    drift_alerts = (
        list(fill.get("alerts") or [])
        + list(sig.get("alerts") or [])
        + list(regime.get("alerts") or [])
    )
    if drift_alerts:
        lines.append("- Active drift alerts:")
        for alert in drift_alerts:
            lines.append(f"  - {alert}")

    rec = report.get("reconciler_summary") or {}
    rec_share = rec.get("reconciled_share")
    lines.extend([
        "",
        "## Orphan-Fill Reconciler",
        f"- Filled today: {rec.get('filled_total', 0)}; "
        f"recovered by reconciler: {rec.get('reconciled_total', 0)} "
        f"({_fmt_pct(rec_share)})."
    ])
    if rec.get("by_source"):
        for source, count in sorted((rec.get("by_source") or {}).items()):
            lines.append(f"  - {source}: {count}")
    for alert in rec.get("alerts") or []:
        lines.append(f"- Alert: {alert}")

    lines.extend(["", "## Notes"])
    notes = report.get("notes") or []
    if notes:
        lines.extend(f"- {note}" for note in notes)
    else:
        lines.append("- No automatic notes.")

    return "\n".join(lines) + "\n"
