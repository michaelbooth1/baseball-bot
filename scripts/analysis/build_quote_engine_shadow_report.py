#!/usr/bin/env python3
"""Phase C shadow report (2026-05-17).

Reads the per-date `<date>_quotes.jsonl` shadow ledgers written by the
two-sided quote engine in shadow mode and summarises what the engine
WOULD have quoted across the trailing window.

This is pure observability: nothing here can place orders. The output
is the operator's window into "if we ran market-maker mode today,
what would it look like?"

Output sections:
  - `coverage`: rows by mode + per-date emission counts. Tells the
    operator whether the engine even ran (artifact_present, n_rows).
  - `quote_emission_rates`: of the rows where the engine COULD have
    quoted (had FV + book + under-pair), what fraction had both sides
    quoted vs single-sided vs neither. Per-skip-reason histogram.
  - `spread_summary`: distribution of (would_quote_ask - would_quote_bid)
    when both sides quoted. The MM thesis depends on capturing this
    spread, so we surface min/p25/p50/p75/max + mean.
  - `inventory_distribution`: distribution of net_inventory_over_shares
    across decisions. Tells the operator how often the engine would
    have been near max-inventory (which truncates one side).
  - `hedge_opportunities`: count + average inventory-at-trigger by
    hedge_side.

Outputs:
  data/analysis_output/quote_engine_shadow/
    quote_engine_shadow_report.json
    quote_engine_shadow_report.md
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import statistics
from collections import Counter
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence


PROJECT_DIR = Path(__file__).resolve().parents[2]
DEFAULT_LIVE_SHADOW_ROOT = (
    PROJECT_DIR / "data" / "live_trading" / "quote_engine_shadow"
)
DEFAULT_PAPER_SHADOW_ROOT = (
    PROJECT_DIR / "data" / "paper_trading" / "quote_engine_shadow"
)
DEFAULT_OUTPUT_ROOT = (
    PROJECT_DIR / "data" / "analysis_output" / "quote_engine_shadow"
)
DEFAULT_TRAILING_DAYS = 7


LOGGER = logging.getLogger("build_quote_engine_shadow_report")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _safe_float(v: Any) -> Optional[float]:
    if v is None or v == "":
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if math.isnan(f) or math.isinf(f):
        return None
    return f


def _read_jsonl(path: Path) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    if not path.exists():
        return rows
    try:
        with open(path, encoding="utf-8") as f:
            for raw in f:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    row = json.loads(raw)
                except (ValueError, json.JSONDecodeError):
                    continue
                if isinstance(row, dict):
                    rows.append(row)
    except OSError as exc:
        LOGGER.warning("Failed to read %s: %s", path, exc)
    return rows


def _percentile(values: Sequence[float], p: float) -> Optional[float]:
    if not values:
        return None
    sorted_vals = sorted(values)
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    k = (len(sorted_vals) - 1) * p
    lo = int(math.floor(k))
    hi = int(math.ceil(k))
    if lo == hi:
        return sorted_vals[lo]
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (k - lo)


def _distribution(values: Sequence[float]) -> Dict[str, Optional[float]]:
    """Compact distribution summary for the report."""
    if not values:
        return {
            "n": 0, "mean": None, "stdev": None,
            "min": None, "p25": None, "p50": None,
            "p75": None, "max": None,
        }
    return {
        "n": len(values),
        "mean": round(statistics.mean(values), 4),
        "stdev": round(statistics.stdev(values), 4) if len(values) > 1 else 0.0,
        "min": round(min(values), 4),
        "p25": round(_percentile(values, 0.25), 4),
        "p50": round(_percentile(values, 0.50), 4),
        "p75": round(_percentile(values, 0.75), 4),
        "max": round(max(values), 4),
    }


def _trailing_dates(today: str, n_days: int) -> List[str]:
    end = date.fromisoformat(today)
    return [
        (end - _td_days(i)).isoformat()
        for i in range(n_days)
    ]


def _td_days(n: int):
    from datetime import timedelta
    return timedelta(days=n)


def _load_window_rows(
    roots: Sequence[Path], dates: Sequence[str]
) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for root in roots:
        if not root.exists():
            continue
        for d in dates:
            path = root / f"{d}_quotes.jsonl"
            file_rows = _read_jsonl(path)
            for r in file_rows:
                r.setdefault("session_date", d)
                r.setdefault("source_root", str(root))
            rows.extend(file_rows)
    return rows


def build_report(
    rows: Sequence[Dict[str, Any]], *, window_days: int = DEFAULT_TRAILING_DAYS,
) -> Dict[str, Any]:
    """Build the shadow-summary payload from one window's rows."""
    # ---- coverage ----
    by_date = Counter(str(r.get("session_date") or "") for r in rows)
    coverage = {
        "n_rows_total": len(rows),
        "n_dates_present": sum(1 for c in by_date.values() if c > 0),
        "rows_per_date": dict(sorted(by_date.items())),
        "window_days_requested": window_days,
    }

    # ---- quote emission ----
    bid_reasons = Counter()
    ask_reasons = Counter()
    n_both_quoted = 0
    n_bid_only = 0
    n_ask_only = 0
    n_neither = 0
    spread_values: List[float] = []
    inventory_values: List[float] = []
    shade_values: List[float] = []
    fv_values: List[float] = []
    for r in rows:
        bid_reason = str(r.get("bid_skipped_reason") or "ok")
        ask_reason = str(r.get("ask_skipped_reason") or "ok")
        bid_reasons[bid_reason] += 1
        ask_reasons[ask_reason] += 1
        bid_price = _safe_float(r.get("would_quote_bid"))
        ask_price = _safe_float(r.get("would_quote_ask"))
        if bid_price is not None and ask_price is not None:
            n_both_quoted += 1
            spread_values.append(round(ask_price - bid_price, 4))
        elif bid_price is not None:
            n_bid_only += 1
        elif ask_price is not None:
            n_ask_only += 1
        else:
            n_neither += 1
        inv = _safe_float(r.get("net_inventory_over_shares"))
        if inv is not None:
            inventory_values.append(inv)
        sh = _safe_float(r.get("inventory_shade"))
        if sh is not None:
            shade_values.append(sh)
        fv = _safe_float(r.get("over_fair_value"))
        if fv is not None:
            fv_values.append(fv)

    quote_emission_rates = {
        "n_both_quoted": n_both_quoted,
        "n_bid_only": n_bid_only,
        "n_ask_only": n_ask_only,
        "n_neither_quoted": n_neither,
        "both_quoted_share": (
            round(n_both_quoted / len(rows), 4) if rows else None
        ),
        "bid_skip_reasons": dict(bid_reasons.most_common()),
        "ask_skip_reasons": dict(ask_reasons.most_common()),
    }

    # ---- hedge opportunities ----
    hedge_hits = [r for r in rows if bool(r.get("hedge_opportunity"))]
    hedge_by_side: Counter = Counter()
    hedge_inventory_at_trigger: List[float] = []
    for r in hedge_hits:
        side = str(r.get("hedge_side") or "")
        if side:
            hedge_by_side[side] += 1
        inv = _safe_float(r.get("net_inventory_over_shares"))
        if inv is not None:
            hedge_inventory_at_trigger.append(inv)

    return {
        "generated_at_utc": _now_iso(),
        "schema_version": 1,
        "phase": "C_shadow",
        "scope": "offline_only_until_paper_validation_clears",
        "coverage": coverage,
        "quote_emission_rates": quote_emission_rates,
        "spread_summary": _distribution(spread_values),
        "inventory_summary": _distribution(inventory_values),
        "shade_summary": _distribution(shade_values),
        "fair_value_summary": _distribution(fv_values),
        "hedge_opportunities": {
            "n_triggered": len(hedge_hits),
            "by_side": dict(hedge_by_side),
            "inventory_at_trigger_summary": _distribution(
                hedge_inventory_at_trigger
            ),
        },
        "notes": {
            "scope": (
                "Pure observability. NO orders are placed by the "
                "quote engine in shadow mode. Live trading remains "
                "Over-only via the existing _place_bet path."
            ),
            "advancement_gate": (
                "Per ROADMAP Phase B4: >= 60 daily sessions in this "
                "mode + paper-mode trading clearance before any Phase "
                "C piece flips to live."
            ),
        },
    }


def _fmt_pct(v: Optional[float]) -> str:
    return "n/a" if v is None else f"{v * 100:.1f}%"


def render_markdown(payload: Dict[str, Any]) -> str:
    lines: List[str] = []
    lines.append("# Quote Engine Shadow Report (Phase C shadow)")
    lines.append("")
    lines.append(f"Generated: {payload['generated_at_utc']}")
    lines.append(f"Phase: **{payload['phase']}**  ")
    lines.append(f"Scope: {payload['scope']}")
    lines.append("")
    cov = payload["coverage"]
    lines.append("## Coverage")
    lines.append(
        f"- N rows: {cov['n_rows_total']}, dates with data: "
        f"{cov['n_dates_present']} (requested {cov['window_days_requested']}d)"
    )
    if cov["rows_per_date"]:
        lines.append("- Rows per date:")
        for d, n in cov["rows_per_date"].items():
            lines.append(f"  - `{d}`: {n}")
    lines.append("")
    qer = payload["quote_emission_rates"]
    lines.append("## Quote emission")
    lines.append(
        f"- Both sides quoted: {qer['n_both_quoted']} "
        f"({_fmt_pct(qer['both_quoted_share'])})"
    )
    lines.append(
        f"- Bid only: {qer['n_bid_only']}; ask only: {qer['n_ask_only']}; "
        f"neither: {qer['n_neither_quoted']}"
    )
    lines.append("- Bid skip reasons:")
    for r, n in qer["bid_skip_reasons"].items():
        lines.append(f"  - `{r}`: {n}")
    lines.append("- Ask skip reasons:")
    for r, n in qer["ask_skip_reasons"].items():
        lines.append(f"  - `{r}`: {n}")
    lines.append("")
    sp = payload["spread_summary"]
    lines.append("## Would-have spread (ask - bid when both sides quoted)")
    lines.append(
        f"- n={sp['n']}, mean={sp['mean']}, p50={sp['p50']}, "
        f"min={sp['min']}, max={sp['max']}"
    )
    lines.append("")
    inv = payload["inventory_summary"]
    lines.append("## Net OVER inventory at decision moments")
    lines.append(
        f"- n={inv['n']}, mean={inv['mean']}, p50={inv['p50']}, "
        f"min={inv['min']}, max={inv['max']}"
    )
    lines.append("")
    sh = payload["shade_summary"]
    lines.append("## Inventory shade applied (signed; + when long)")
    lines.append(
        f"- n={sh['n']}, mean={sh['mean']}, p50={sh['p50']}, "
        f"min={sh['min']}, max={sh['max']}"
    )
    lines.append("")
    hh = payload["hedge_opportunities"]
    lines.append("## Hedge opportunities")
    lines.append(f"- N triggered: {hh['n_triggered']}")
    if hh.get("by_side"):
        for side, n in hh["by_side"].items():
            lines.append(f"  - `{side}`: {n}")
    lines.append("")
    lines.append("## Notes")
    for k, v in (payload.get("notes") or {}).items():
        lines.append(f"- **{k}**: {v}")
    return "\n".join(lines) + "\n"


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Summarise the two-sided quote engine's shadow ledger."
    )
    p.add_argument(
        "--mode", choices=["live", "paper", "both"], default="both",
    )
    p.add_argument(
        "--today", type=str, default="",
        help="YYYY-MM-DD end date for the trailing window. "
             "Defaults to the latest date with any shadow ledger.",
    )
    p.add_argument(
        "--window-days", type=int, default=DEFAULT_TRAILING_DAYS,
    )
    p.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    return p.parse_args(argv)


def _resolve_today(roots: Sequence[Path]) -> Optional[str]:
    """Pick the most recent ledger date across the provided roots.
    Returns None when no shadow files exist (the day-zero case)."""
    candidates: List[str] = []
    for root in roots:
        if not root.exists():
            continue
        for path in root.glob("*_quotes.jsonl"):
            name = path.name
            if len(name) >= 10:
                candidates.append(name[:10])
    if not candidates:
        return None
    return max(candidates)


def main(argv: Optional[List[str]] = None) -> None:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s %(message)s",
    )

    roots: List[Path] = []
    if args.mode in ("live", "both"):
        roots.append(DEFAULT_LIVE_SHADOW_ROOT)
    if args.mode in ("paper", "both"):
        roots.append(DEFAULT_PAPER_SHADOW_ROOT)

    today = args.today or _resolve_today(roots)
    if today is None:
        # Day-zero: no shadow ledger yet. Emit an empty report so
        # downstream consumers don't trip on a missing file.
        today = datetime.now(timezone.utc).date().isoformat()
        rows: List[Dict[str, Any]] = []
    else:
        dates = _trailing_dates(today, args.window_days)
        rows = _load_window_rows(roots, dates)

    payload = build_report(rows, window_days=args.window_days)
    payload["window_end_date"] = today
    payload["config"] = {
        "mode": args.mode,
        "window_days": args.window_days,
    }

    args.output_root.mkdir(parents=True, exist_ok=True)
    json_path = args.output_root / "quote_engine_shadow_report.json"
    md_path = args.output_root / "quote_engine_shadow_report.md"
    json_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    md_path.write_text(render_markdown(payload), encoding="utf-8")
    LOGGER.info(
        "Wrote %s (n_rows=%d, both_quoted=%d)",
        json_path, payload["coverage"]["n_rows_total"],
        payload["quote_emission_rates"]["n_both_quoted"],
    )


if __name__ == "__main__":
    main()
