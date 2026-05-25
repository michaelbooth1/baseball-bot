"""Markdown renderer for the model-maturity report.

Extracted from build_model_maturity_report.py on 2026-05-25 (Tier 2).
Pure render. Public surface re-exported by the original module.
"""
from __future__ import annotations

from typing import Any, List, Mapping, Optional


def _safe_float(value: Any) -> Optional[float]:
    """Local helper, mirrors the one in build_model_maturity_report.py.
    Kept here to keep this module self-contained."""
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f


def _fmt_metric(value: Any, digits: int = 3) -> str:
    value_f = _safe_float(value)
    if value_f is None:
        return "n/a"
    return f"{value_f:.{digits}f}"


def _fmt_pct(value: Any) -> str:
    value_f = _safe_float(value)
    if value_f is None:
        return "n/a"
    return f"{value_f * 100:.1f}%"


def _markdown_table(headers: List[str], rows: List[List[Any]]) -> List[str]:
    out = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    for row in rows:
        out.append("| " + " | ".join(str(cell) for cell in row) + " |")
    return out


def _roi_bucket_rows(bucket_map: Mapping[str, Any]) -> List[List[Any]]:
    rows: List[List[Any]] = []
    for bucket, payload in bucket_map.items():
        rows.append([
            bucket,
            int(payload.get("rows") or 0),
            int(payload.get("wins") or 0),
            int(payload.get("losses") or 0),
            _fmt_pct(payload.get("win_rate")),
            _fmt_metric(payload.get("profit_units")),
            _fmt_pct(payload.get("roi_per_1_usdc")),
        ])
    return rows or [["none", 0, 0, 0, "n/a", "n/a", "n/a"]]


def _coverage_rows(coverage_map: Mapping[str, Any]) -> List[List[Any]]:
    rows: List[List[Any]] = []
    for family, payload in coverage_map.items():
        rows.append([
            family,
            int((payload or {}).get("rows") or 0),
            _fmt_pct((payload or {}).get("under_pair_available_rate")),
            _fmt_pct((payload or {}).get("no_vig_market_rate")),
            _fmt_pct((payload or {}).get("run_count_panel_rate")),
            _fmt_pct((payload or {}).get("run_count_panel_full_3run_rate")),
            _fmt_pct((payload or {}).get("inferred_poisson_empirical_rate")),
            _fmt_pct((payload or {}).get("stage1_support_rate")),
            _fmt_pct((payload or {}).get("current_state_support_rate")),
        ])
    return rows or [["none", 0, "n/a", "n/a", "n/a", "n/a", "n/a", "n/a", "n/a"]]


def render_markdown(report: Mapping[str, Any]) -> str:
    lines: List[str] = [
        "# MLB Polymarket Model Maturity Report",
        "",
        f"- Generated: {report.get('generated_at_utc')}",
        f"- Mode: {report.get('mode')}",
        f"- Overall status: {report.get('overall_status')}",
        f"- Source rows: {(report.get('source') or {}).get('filtered_rows')} filtered / {(report.get('source') or {}).get('source_rows')} loaded",
        f"- Date range: {(report.get('source') or {}).get('first_date')} -> {(report.get('source') or {}).get('last_date')}",
        "",
        "## Family Readiness",
    ]

    readiness_rows: List[List[Any]] = []
    for family, payload in (report.get("families") or {}).items():
        counts = payload.get("counts") or {}
        promo = ((payload.get("artifact_promotability") or {}).get("probability_calibration") or {})
        readiness_rows.append([
            family,
            counts.get("rows", 0),
            counts.get("dates", 0),
            counts.get("final_label_rows", 0),
            counts.get("positive_final_labels", 0),
            counts.get("negative_final_labels", 0),
            counts.get("score_confirmation_rows", 0),
            promo.get("status", "unknown"),
        ])
    lines.extend(_markdown_table(
        ["Family", "Rows", "Dates", "Settled", "Wins", "Losses", "Score Conf.", "Status"],
        readiness_rows,
    ))

    coverage = report.get("coverage_checks") or {}
    lines.extend(["", "## Coverage Checks"])
    lines.extend(_markdown_table(
        [
            "Family",
            "Rows",
            "Under Pair",
            "No-Vig Market",
            "Run Panel",
            "Full 3-Run Panel",
            "Poisson+Empirical",
            "Stage-1 Support",
            "Current Support",
        ],
        _coverage_rows((coverage.get("by_family") or {})),
    ))

    for family, payload in (report.get("families") or {}).items():
        lines.extend(["", f"## {family}", "", "### Probability Metrics"])
        metric_rows: List[List[Any]] = []
        for metric_name, metric in (payload.get("metrics") or {}).items():
            metric_rows.append([
                metric_name,
                metric.get("n", 0),
                _fmt_metric(metric.get("brier")),
                _fmt_metric(metric.get("logloss")),
                _fmt_metric(metric.get("auc")),
                _fmt_metric(metric.get("calibration_intercept")),
                _fmt_metric(metric.get("calibration_slope")),
                metric.get("calibration_status"),
            ])
        lines.extend(_markdown_table(
            ["Metric", "N", "Brier", "Logloss", "AUC", "Cal Int.", "Cal Slope", "Cal Status"],
            metric_rows,
        ))

        roi = payload.get("roi") or {}
        overall_taker = ((roi.get("overall") or {}).get("taker") or {})
        lines.extend([
            "",
            "### ROI",
            f"- Overall taker ROI: {_fmt_pct(overall_taker.get('roi_per_1_usdc'))} "
            f"({overall_taker.get('rows', 0)} settled rows, profit units {_fmt_metric(overall_taker.get('profit_units'))})",
            "",
            "Ask bucket:",
        ])
        ask = (((roi.get("by_ask_bucket") or {}).get("taker") or {}))
        lines.extend(_markdown_table(
            ["Bucket", "Rows", "Wins", "Losses", "Win Rate", "Profit Units", "ROI"],
            _roi_bucket_rows(ask),
        ))
        lines.extend(["", "Current-state-edge bucket:"])
        current_edge = (((roi.get("by_current_state_edge_bucket") or {}).get("taker") or {}))
        lines.extend(_markdown_table(
            ["Bucket", "Rows", "Wins", "Losses", "Win Rate", "Profit Units", "ROI"],
            _roi_bucket_rows(current_edge),
        ))
        lines.extend(["", "Phantom/confirmation bucket:"])
        phantom = (((roi.get("by_phantom_confirmation_bucket") or {}).get("taker") or {}))
        lines.extend(_markdown_table(
            ["Bucket", "Rows", "Wins", "Losses", "Win Rate", "Profit Units", "ROI"],
            _roi_bucket_rows(phantom),
        ))

        lines.extend(["", "### Promotion"])
        for artifact, status in (payload.get("artifact_promotability") or {}).items():
            lines.append(f"- {artifact}: {status.get('status')} (promotable={bool(status.get('promotable'))})")
            for reason in status.get("reasons") or []:
                lines.append(f"  - {reason}")

    lines.extend(["", "## Warnings"])
    warnings = report.get("warnings") or []
    if warnings:
        lines.extend(f"- {warning}" for warning in warnings)
    else:
        lines.append("- None.")
    return "\n".join(lines) + "\n"


