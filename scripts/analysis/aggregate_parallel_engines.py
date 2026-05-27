#!/usr/bin/env python3
"""aggregate_parallel_engines.py -- Compare parallel paper engine roots.

Reads isolated paper roots produced by scripts/trading/launch_parallel_engines.py
and emits a compact Markdown + JSON comparison. This intentionally reads the
paper roots directly instead of relying on the canonical unified table, whose
paper mode still points at data/paper_trading by default.
"""

from __future__ import annotations

import argparse
import json
import math
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple


PROJECT_DIR = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT_ROOT = PROJECT_DIR / "data" / "analysis_output" / "parallel_engine_comparison"


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _safe_float(v: Any) -> Optional[float]:
    try:
        if v is None or v == "":
            return None
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _safe_bool(v: Any) -> Optional[bool]:
    if v is None or v == "":
        return None
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v)
    text = str(v).strip().lower()
    if text in {"true", "1", "yes", "y"}:
        return True
    if text in {"false", "0", "no", "n"}:
        return False
    return None


def _date_from_path(path: Path) -> str:
    name = path.name
    return name[:10] if len(name) >= 10 and name[4] == "-" and name[7] == "-" else ""


def _in_date_range(date_str: str, start: str, end: str) -> bool:
    if not date_str:
        return True
    if start and date_str < start:
        return False
    if end and date_str > end:
        return False
    return True


def _read_json(path: Path) -> Dict[str, Any]:
    with open(path, encoding="utf-8") as f:
        payload = json.load(f)
    return payload if isinstance(payload, dict) else {}


def _read_jsonl(path: Path) -> Iterable[Dict[str, Any]]:
    with open(path, encoding="utf-8") as f:
        for raw in f:
            raw = raw.strip()
            if not raw:
                continue
            try:
                obj = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                yield obj


def _max_drawdown(profits: Sequence[float]) -> float:
    peak = 0.0
    running = 0.0
    worst = 0.0
    for profit in profits:
        running += profit
        peak = max(peak, running)
        worst = min(worst, running - peak)
    return round(worst, 4)


def _mean(values: Sequence[float]) -> Optional[float]:
    return sum(values) / len(values) if values else None


def _weighted_mean(values: Sequence[Tuple[float, float]]) -> Optional[float]:
    total_weight = sum(w for _, w in values if w > 0)
    if total_weight <= 0:
        return None
    return sum(v * w for v, w in values if w > 0) / total_weight


def _fmt_money(v: Optional[float]) -> str:
    return "--" if v is None else f"${v:+,.2f}"


def _fmt_pct(v: Optional[float]) -> str:
    return "--" if v is None else f"{v * 100:.1f}%"


def _fmt_num(v: Optional[float]) -> str:
    return "--" if v is None else f"{v:.3f}"


def _outcome_key(row: Dict[str, Any]) -> str:
    existing = str(row.get("outcome_join_key") or "")
    if existing:
        return existing
    date_str = str(row.get("session_date") or row.get("date") or row.get("game_date") or "")
    return f"{date_str}|{row.get('game_pk')}|{row.get('line')}"


def _fine_candidate_key(row: Dict[str, Any]) -> str:
    pieces = [
        _outcome_key(row),
        str(row.get("inning") or ""),
        str(row.get("inning_state") or ""),
        str(row.get("outs") or ""),
        str(row.get("runners_on") or ""),
        str(row.get("away_score_before") or ""),
        str(row.get("home_score_before") or ""),
    ]
    return "|".join(pieces)


def _decision(row: Dict[str, Any]) -> str:
    raw = str(row.get("decision") or "").strip().lower()
    if raw == "trade" or row.get("bet_id"):
        return "trade"
    if raw:
        return raw
    return "skip"


def _root_label(root: Path, sessions: List[Dict[str, Any]]) -> str:
    labels = Counter()
    for session in sessions:
        params = session.get("params") or {}
        label = str(params.get("config_label") or "")
        if label:
            labels[label] += 1
    if labels:
        return labels.most_common(1)[0][0]
    name = root.name
    return name[6:] if name.startswith("paper_") else name


def _load_sessions(root: Path, start: str, end: str) -> List[Dict[str, Any]]:
    sessions_dir = root / "sessions"
    out: List[Dict[str, Any]] = []
    for path in sorted(sessions_dir.glob("*_session.json")):
        date_str = _date_from_path(path)
        if not _in_date_range(date_str, start, end):
            continue
        payload = _read_json(path)
        payload["_path"] = str(path)
        payload["_date"] = date_str
        out.append(payload)
    return out


def _load_candidates(root: Path, start: str, end: str) -> List[Dict[str, Any]]:
    cand_dir = root / "candidate_universe"
    rows: List[Dict[str, Any]] = []
    for path in sorted(cand_dir.glob("*_candidates.jsonl")):
        date_str = _date_from_path(path)
        if not _in_date_range(date_str, start, end):
            continue
        for row in _read_jsonl(path):
            row.setdefault("session_date", date_str)
            rows.append(row)
    return rows


def _load_outcomes(root: Path, start: str, end: str) -> Dict[str, Dict[str, Any]]:
    cand_dir = root / "candidate_universe"
    out: Dict[str, Dict[str, Any]] = {}
    for path in sorted(cand_dir.glob("*_outcomes.jsonl")):
        date_str = _date_from_path(path)
        if not _in_date_range(date_str, start, end):
            continue
        for row in _read_jsonl(path):
            row.setdefault("session_date", date_str)
            out[_outcome_key(row)] = row
    return out


def _bet_metrics(sessions: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    bets: List[Dict[str, Any]] = []
    for session in sessions:
        for bet in session.get("bets") or []:
            if isinstance(bet, dict):
                bets.append(bet)

    settled = [b for b in bets if _safe_bool(b.get("settled"))]
    wins = [b for b in settled if _safe_bool(b.get("won"))]
    profits = [_safe_float(b.get("profit")) or 0.0 for b in sorted(settled, key=lambda b: str(b.get("placed_at") or ""))]
    total_profit = round(sum(profits), 4)
    total_staked = sum(_safe_float(b.get("stake")) or 0.0 for b in settled)
    mean_fv_vals = [_safe_float(b.get("fair_value")) for b in bets]
    mean_ask_vals = [_safe_float(b.get("entry_ask")) for b in bets]
    mean_fv_vals = [v for v in mean_fv_vals if v is not None]
    mean_ask_vals = [v for v in mean_ask_vals if v is not None]
    mean_fv = _mean(mean_fv_vals)
    mean_ask = _mean(mean_ask_vals)
    settled_fv_vals = [_safe_float(b.get("fair_value")) for b in settled]
    settled_ask_vals = [_safe_float(b.get("entry_ask")) for b in settled]
    settled_fv_vals = [v for v in settled_fv_vals if v is not None]
    settled_ask_vals = [v for v in settled_ask_vals if v is not None]
    mean_fv_settled = _mean(settled_fv_vals)
    mean_ask_settled = _mean(settled_ask_vals)
    weighted_ask_pairs: List[Tuple[float, float]] = []
    weighted_fv_pairs: List[Tuple[float, float]] = []
    weighted_win_pairs: List[Tuple[float, float]] = []
    for b in settled:
        stake = _safe_float(b.get("stake")) or 0.0
        if stake <= 0:
            continue
        ask = _safe_float(b.get("entry_ask"))
        fv = _safe_float(b.get("fair_value"))
        won = _safe_bool(b.get("won"))
        if ask is not None:
            weighted_ask_pairs.append((ask, stake))
        if fv is not None:
            weighted_fv_pairs.append((fv, stake))
        if won is not None:
            weighted_win_pairs.append((1.0 if won else 0.0, stake))
    stake_weighted_ask = _weighted_mean(weighted_ask_pairs)
    stake_weighted_fv = _weighted_mean(weighted_fv_pairs)
    stake_weighted_wr = _weighted_mean(weighted_win_pairs)
    actual_wr = len(wins) / len(settled) if settled else None

    # 2026-05-26 normalization: profit_per_settled_bet and
    # bets_per_unique_game_line make F_no_dedup (which can place 5-10x
    # more bets than A_current on the same slate) readable against A
    # on equal per-bet footing. Without these, the headline P&L and
    # stake columns favor whichever config trades most regardless of
    # quality.
    profit_per_settled_bet = (
        round(total_profit / len(settled), 4) if settled else None
    )
    # Unique (game_pk, line) over ALL bets (including unsettled), so
    # the denominator reflects cohort breadth, not realization timing.
    unique_game_lines: set[Tuple[Any, Any]] = set()
    for b in bets:
        gpk = b.get("game_pk")
        ln = b.get("line")
        if gpk is not None and ln is not None:
            unique_game_lines.add((gpk, ln))
    n_unique_game_lines = len(unique_game_lines)
    bets_per_unique_game_line = (
        round(len(bets) / n_unique_game_lines, 4)
        if n_unique_game_lines > 0 else None
    )
    # Settled-bets cohort breadth tracks "how many distinct game-lines
    # actually produced realized outcomes" — useful for F's
    # bet-multiple-times-on-same-line pattern (settled cohort may be
    # narrower than n_settled suggests if many bets share a line).
    settled_unique_game_lines: set[Tuple[Any, Any]] = set()
    for b in settled:
        gpk = b.get("game_pk")
        ln = b.get("line")
        if gpk is not None and ln is not None:
            settled_unique_game_lines.add((gpk, ln))
    n_settled_unique_game_lines = len(settled_unique_game_lines)

    return {
        "n_bets": len(bets),
        "n_settled": len(settled),
        "n_won": len(wins),
        "win_rate": actual_wr,
        "total_staked": round(total_staked, 4),
        "total_profit": total_profit,
        "roi": round(total_profit / total_staked, 6) if total_staked > 0 else None,
        "max_drawdown": _max_drawdown(profits),
        # Normalization metrics (per-bet / per-cohort breadth):
        "profit_per_settled_bet": profit_per_settled_bet,
        "n_unique_game_lines": n_unique_game_lines,
        "n_settled_unique_game_lines": n_settled_unique_game_lines,
        "bets_per_unique_game_line": bets_per_unique_game_line,
        "mean_fair_value": round(mean_fv, 6) if mean_fv is not None else None,
        "mean_entry_ask": round(mean_ask, 6) if mean_ask is not None else None,
        "mean_fair_value_settled": round(mean_fv_settled, 6) if mean_fv_settled is not None else None,
        "mean_entry_ask_settled": round(mean_ask_settled, 6) if mean_ask_settled is not None else None,
        "stake_weighted_fair_value": round(stake_weighted_fv, 6) if stake_weighted_fv is not None else None,
        "stake_weighted_entry_ask": round(stake_weighted_ask, 6) if stake_weighted_ask is not None else None,
        "stake_weighted_win_rate": round(stake_weighted_wr, 6) if stake_weighted_wr is not None else None,
        "edge_over_market_actual_minus_ask": (
            round(actual_wr - mean_ask, 6)
            if actual_wr is not None and mean_ask is not None else None
        ),
        "edge_over_market_settled_actual_minus_ask": (
            round(actual_wr - mean_ask_settled, 6)
            if actual_wr is not None and mean_ask_settled is not None else None
        ),
        "edge_over_market_stake_weighted_actual_minus_ask": (
            round(stake_weighted_wr - stake_weighted_ask, 6)
            if stake_weighted_wr is not None and stake_weighted_ask is not None else None
        ),
    }


def _candidate_metrics(rows: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    by_decision = Counter(_decision(r) for r in rows)
    by_reason = Counter(str(r.get("decision_reason") or "missing") for r in rows)
    by_strategy = Counter(str(r.get("state_value_strategy") or r.get("signal_model_family") or "missing") for r in rows)
    return {
        "n_candidates": len(rows),
        "by_decision": dict(sorted(by_decision.items())),
        "top_decision_reasons": dict(by_reason.most_common(25)),
        "by_strategy": dict(sorted(by_strategy.items())),
    }


def _config_completeness(root: Path, sessions: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    if not sessions:
        return {
            "complete": False,
            "missing_session": True,
            "reasons": ["missing_session"],
        }
    reasons: List[str] = []
    total_gaps = 0
    total_disconnects = 0
    max_lag = 0.0
    last_sequence = 0
    shared_sessions = 0
    shutdown_seen = False
    for session in sessions:
        params = session.get("params") or {}
        summary = session.get("summary") or {}
        mode = str(params.get("market_data_mode") or "")
        health = summary.get("market_data_health") or {}
        if mode == "shared_consumer":
            shared_sessions += 1
        total_gaps += int(summary.get("market_data_gap_count") or health.get("market_data_gap_count") or 0)
        total_disconnects += int(summary.get("consumer_disconnects") or health.get("consumer_disconnects") or 0)
        max_lag = max(max_lag, float(summary.get("max_market_data_lag_ms") or health.get("max_market_data_lag_ms") or 0.0))
        last_sequence = max(last_sequence, int(summary.get("last_market_data_sequence") or health.get("last_market_data_sequence") or 0))
        shutdown_seen = shutdown_seen or bool(health.get("shutdown_received"))
    if total_gaps > 0:
        reasons.append("market_data_sequence_gaps")
    if total_disconnects > 0:
        reasons.append("consumer_disconnects")
    if shared_sessions > 0 and not shutdown_seen:
        reasons.append("shared_watcher_shutdown_not_seen")
    if max_lag >= 30_000:
        reasons.append("large_market_data_lag")
    return {
        "complete": not reasons,
        "missing_session": False,
        "reasons": reasons,
        "market_data_gap_count": total_gaps,
        "consumer_disconnects": total_disconnects,
        "max_market_data_lag_ms": round(max_lag, 2),
        "last_market_data_sequence": last_sequence,
        "shared_session_count": shared_sessions,
        "root": str(root),
    }


def _decision_maps(config_payloads: Dict[str, Dict[str, Any]], *, fine: bool = False) -> Dict[str, Dict[str, str]]:
    by_key: Dict[str, Dict[str, str]] = defaultdict(dict)
    for label, payload in config_payloads.items():
        candidates = payload.get("candidates") or []
        per_config: Dict[str, str] = {}
        for row in candidates:
            key = _fine_candidate_key(row) if fine else _outcome_key(row)
            decision = _decision(row)
            prior = per_config.get(key)
            if prior == "trade":
                continue
            per_config[key] = "trade" if decision == "trade" else "skip"
        for key, decision in per_config.items():
            by_key[key][label] = decision
    return by_key


def _shared_disagreement(config_payloads: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    labels = sorted(config_payloads.keys())
    game_line = _decision_maps(config_payloads, fine=False)
    fine_state = _decision_maps(config_payloads, fine=True)
    outcomes: Dict[str, Dict[str, Any]] = {}
    for payload in config_payloads.values():
        outcomes.update(payload.get("outcomes") or {})

    def outcome_for(key: str) -> Dict[str, Any]:
        row = outcomes.get(key)
        if row is None:
            base_key = "|".join(key.split("|")[:3])
            row = outcomes.get(base_key)
        if not row:
            return {}
        return {
            "won": row.get("won") if row.get("won") is not None else row.get("target_counterfactual_win"),
            "final_total": row.get("final_total"),
            "final_away": row.get("final_away"),
            "final_home": row.get("final_home"),
        }

    def summarize(mapping: Dict[str, Dict[str, str]]) -> Tuple[Dict[str, int], List[Dict[str, Any]]]:
        counts = Counter()
        splits: List[Dict[str, Any]] = []
        for key, decisions in sorted(mapping.items()):
            if len(decisions) < 2:
                counts["partial_coverage"] += 1
                continue
            vals = {decisions.get(label, "missing") for label in labels}
            if len(vals) == 1:
                only = next(iter(vals))
                counts[f"unanimous_{only}"] += 1
            else:
                counts["split"] += 1
                if len(splits) < 100:
                    splits.append({
                        "key": key,
                        "decisions": {label: decisions.get(label, "missing") for label in labels},
                        "outcome": outcome_for(key),
                    })
        counts["keys_compared"] = len(mapping)
        return dict(counts), splits

    game_line_counts, game_line_splits = summarize(game_line)
    fine_counts, fine_splits = summarize(fine_state)
    return {
        "labels": labels,
        "game_line": {"counts": game_line_counts, "split_examples": game_line_splits},
        "fine_state": {"counts": fine_counts, "split_examples": fine_splits},
    }


def build_report(roots: Sequence[Path], start: str, end: str) -> Dict[str, Any]:
    configs: Dict[str, Dict[str, Any]] = {}
    for root in roots:
        sessions = _load_sessions(root, start, end)
        candidates = _load_candidates(root, start, end)
        outcomes = _load_outcomes(root, start, end)
        label = _root_label(root, sessions)
        if label in configs:
            label = f"{label}_{len(configs) + 1}"
        configs[label] = {
            "root": str(root),
            "sessions": sessions,
            "candidates": candidates,
            "outcomes": outcomes,
            "headline": _bet_metrics(sessions),
            "candidate_funnel": _candidate_metrics(candidates),
            "completeness": _config_completeness(root, sessions),
        }

    # 2026-05-26 normalization: stamp volume_index_vs_baseline on
    # every config's headline so per-bet metrics are readable next to
    # raw P&L. Baseline = A_current when present (it's the production-
    # mirror), else the first config alphabetically. Lets the operator
    # immediately spot "F is 8x A in volume" without doing arithmetic.
    baseline_label = (
        "A_current" if "A_current" in configs
        else (sorted(configs.keys())[0] if configs else None)
    )
    baseline_n_bets = 0
    baseline_n_settled = 0
    if baseline_label is not None:
        bh = configs[baseline_label]["headline"]
        baseline_n_bets = int(bh.get("n_bets") or 0)
        baseline_n_settled = int(bh.get("n_settled") or 0)
    for label, payload in configs.items():
        h = payload["headline"]
        h["baseline_label"] = baseline_label
        n_bets = int(h.get("n_bets") or 0)
        n_settled = int(h.get("n_settled") or 0)
        h["volume_index_vs_baseline"] = (
            round(n_bets / baseline_n_bets, 4)
            if baseline_n_bets > 0 else None
        )
        h["settled_index_vs_baseline"] = (
            round(n_settled / baseline_n_settled, 4)
            if baseline_n_settled > 0 else None
        )

    public_configs = {
        label: {
            "root": payload["root"],
            "headline": payload["headline"],
            "candidate_funnel": payload["candidate_funnel"],
            "completeness": payload["completeness"],
        }
        for label, payload in configs.items()
    }
    report = {
        "schema_version": 1,
        "generated_at_utc": _now_iso(),
        "date_range": {"start": start or None, "end": end or None},
        "baseline_config_label": baseline_label,
        "configs": public_configs,
        "shared_candidate_disagreement": _shared_disagreement(configs),
    }
    report["daily_read"] = _daily_read(report)
    return report


def _daily_read(report: Dict[str, Any]) -> Dict[str, Any]:
    configs = report.get("configs") or {}
    ranked_roi: List[Tuple[str, float]] = []
    ranked_dd: List[Tuple[str, float]] = []
    ranked_profit_per_bet: List[Tuple[str, float]] = []
    sample_flags: List[str] = []
    for label, payload in configs.items():
        h = payload.get("headline") or {}
        complete = payload.get("completeness") or {}
        if complete and not complete.get("complete", True):
            sample_flags.append(f"{label}: incomplete ({', '.join(complete.get('reasons') or ['unknown'])})")
        settled = int(h.get("n_settled") or 0)
        if settled < 20:
            sample_flags.append(f"{label}: only {settled} settled bets")
        roi = h.get("roi")
        dd = h.get("max_drawdown")
        ppb = h.get("profit_per_settled_bet")
        if roi is not None:
            ranked_roi.append((label, float(roi)))
        if dd is not None:
            ranked_dd.append((label, float(dd)))
        if ppb is not None and settled >= 3:
            ranked_profit_per_bet.append((label, float(ppb)))
    ranked_roi.sort(key=lambda x: x[1], reverse=True)
    ranked_dd.sort(key=lambda x: x[1], reverse=True)
    ranked_profit_per_bet.sort(key=lambda x: x[1], reverse=True)
    sd = report.get("shared_candidate_disagreement") or {}
    game_line_counts = ((sd.get("game_line") or {}).get("counts") or {})
    fine_counts = ((sd.get("fine_state") or {}).get("counts") or {})
    return {
        "best_roi_config": ranked_roi[0][0] if ranked_roi else None,
        "best_roi": ranked_roi[0][1] if ranked_roi else None,
        "lowest_drawdown_config": ranked_dd[0][0] if ranked_dd else None,
        "lowest_drawdown": ranked_dd[0][1] if ranked_dd else None,
        # 2026-05-26: per-bet ranking helps spot configs that earn
        # more per individual bet (quality) vs configs that just place
        # many bets (volume). F_no_dedup will dominate ROI on volume
        # alone; profit_per_settled_bet exposes whether each bet is
        # actually profitable.
        "best_profit_per_settled_bet_config": (
            ranked_profit_per_bet[0][0] if ranked_profit_per_bet else None
        ),
        "best_profit_per_settled_bet": (
            ranked_profit_per_bet[0][1] if ranked_profit_per_bet else None
        ),
        "baseline_config_label": report.get("baseline_config_label"),
        "game_line_splits": int(game_line_counts.get("split") or 0),
        "fine_state_splits": int(fine_counts.get("split") or 0),
        "sample_flags": sample_flags,
    }


def render_markdown(report: Dict[str, Any]) -> str:
    lines: List[str] = []
    lines.append("# Parallel Engine Comparison\n")
    dr = report.get("date_range") or {}
    lines.append(f"_Generated {report['generated_at_utc']} for {dr.get('start') or 'first'} to {dr.get('end') or 'last'}._\n")
    read = report.get("daily_read") or _daily_read(report)
    lines.append("## Daily read\n")
    baseline_label = read.get("baseline_config_label") or "(none)"
    lines.append(f"- Baseline config (for volume index): `{baseline_label}`")
    lines.append(
        f"- Best ROI so far: `{read.get('best_roi_config') or '--'}` "
        f"({_fmt_pct(read.get('best_roi'))})."
    )
    if read.get("best_profit_per_settled_bet_config"):
        lines.append(
            f"- Best **profit per settled bet**: "
            f"`{read.get('best_profit_per_settled_bet_config')}` "
            f"({_fmt_money(read.get('best_profit_per_settled_bet'))})."
            " (Reads quality independent of volume — useful when F_no_dedup "
            "trades 5-10x more than A.)"
        )
    lines.append(
        f"- Lowest drawdown so far: `{read.get('lowest_drawdown_config') or '--'}` "
        f"({_fmt_money(read.get('lowest_drawdown'))})."
    )
    lines.append(
        f"- Split opportunities: game-line={read.get('game_line_splits', 0)}, "
        f"fine-state={read.get('fine_state_splits', 0)}."
    )
    if read.get("sample_flags"):
        lines.append(
            "- Sample warning: "
            + "; ".join(str(v) for v in read.get("sample_flags") or [])
            + ". Treat rankings as diagnostic, not promotion evidence."
        )
    else:
        lines.append("- Sample warning: settled counts are no longer tiny, but conclusions still need walk-forward/cert support.")
    lines.append("")
    lines.append("## Completeness\n")
    lines.append("| Config | Complete | Reasons | Last Seq | Gaps | Disconnects | Max Lag |")
    lines.append("| --- | --- | --- | ---: | ---: | ---: | ---: |")
    for label, payload in (report.get("configs") or {}).items():
        c = payload.get("completeness") or {}
        reasons = ", ".join(c.get("reasons") or [])
        lines.append(
            f"| `{label}` | {bool(c.get('complete', True))} | "
            f"{reasons or '--'} | {c.get('last_market_data_sequence', 0)} | "
            f"{c.get('market_data_gap_count', 0)} | {c.get('consumer_disconnects', 0)} | "
            f"{c.get('max_market_data_lag_ms', 0.0)}ms |"
        )
    lines.append("")
    lines.append("## Per-config headline\n")
    lines.append("| Config | Bets | Settled | W-L | WR | Stake | P&L | ROI | Max DD | Mean FV | Mean Ask | Settled Actual-Ask | Stake-wtd Actual-Ask |")
    lines.append("| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |")
    for label, payload in (report.get("configs") or {}).items():
        h = payload.get("headline") or {}
        losses = (h.get("n_settled") or 0) - (h.get("n_won") or 0)
        lines.append(
            f"| `{label}` | {h.get('n_bets', 0)} | {h.get('n_settled', 0)} | "
            f"{h.get('n_won', 0)}-{losses} | {_fmt_pct(h.get('win_rate'))} | "
            f"${h.get('total_staked', 0):,.2f} | {_fmt_money(h.get('total_profit'))} | "
            f"{_fmt_pct(h.get('roi'))} | {_fmt_money(h.get('max_drawdown'))} | "
            f"{_fmt_num(h.get('mean_fair_value'))} | "
            f"{_fmt_num(h.get('mean_entry_ask'))} | "
            f"{_fmt_pct(h.get('edge_over_market_settled_actual_minus_ask'))} | "
            f"{_fmt_pct(h.get('edge_over_market_stake_weighted_actual_minus_ask'))} |"
        )

    # 2026-05-26: per-bet / per-cohort normalization table. Reads
    # configs on equal footing regardless of how many bets each
    # placed -- the loose-dedup F_no_dedup config is expected to
    # outvolume A_current by 5-10x on the same slate, so raw P&L
    # comparisons there are misleading.
    lines.append("")
    lines.append("## Per-config normalized (per-bet + cohort breadth)\n")
    lines.append(
        "_Baseline for volume index = `"
        + str(report.get("baseline_config_label") or "(none)")
        + "`. Volume Idx > 1 means the config placed more bets than the baseline; "
        "use it together with **$/Bet** (profit_per_settled_bet) to separate "
        "quality from volume._\n"
    )
    lines.append(
        "| Config | Bets | Settled | Unique GLs | Bets/GL | $/Bet | Volume Idx | Settled Idx |"
    )
    lines.append(
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |"
    )
    for label, payload in (report.get("configs") or {}).items():
        h = payload.get("headline") or {}
        ppb = h.get("profit_per_settled_bet")
        ppb_str = _fmt_money(ppb) if ppb is not None else "—"
        vol_idx = h.get("volume_index_vs_baseline")
        settled_idx = h.get("settled_index_vs_baseline")
        vol_str = f"{vol_idx:.2f}x" if vol_idx is not None else "—"
        settled_str = f"{settled_idx:.2f}x" if settled_idx is not None else "—"
        bets_per_gl = h.get("bets_per_unique_game_line")
        bpgl_str = f"{bets_per_gl:.2f}" if bets_per_gl is not None else "—"
        lines.append(
            f"| `{label}` | {h.get('n_bets', 0)} | {h.get('n_settled', 0)} | "
            f"{h.get('n_unique_game_lines', 0)} | {bpgl_str} | {ppb_str} | "
            f"{vol_str} | {settled_str} |"
        )

    lines.append("\n## Gate/candidate funnel\n")
    for label, payload in (report.get("configs") or {}).items():
        funnel = payload.get("candidate_funnel") or {}
        lines.append(f"### `{label}`\n")
        lines.append(f"- Candidates: **{funnel.get('n_candidates', 0)}**")
        lines.append(f"- Decisions: `{json.dumps(funnel.get('by_decision') or {}, sort_keys=True)}`")
        top = funnel.get("top_decision_reasons") or {}
        if top:
            lines.append("- Top reasons:")
            for reason, count in list(top.items())[:12]:
                lines.append(f"  - `{reason}`: {count}")
        lines.append("")

    lines.append("## Shared-candidate disagreement\n")
    sd = report.get("shared_candidate_disagreement") or {}
    for scope in ("game_line", "fine_state"):
        block = sd.get(scope) or {}
        lines.append(f"### {scope}\n")
        lines.append(f"`{json.dumps(block.get('counts') or {}, sort_keys=True)}`\n")
        examples = block.get("split_examples") or []
        if examples:
            lines.append("| Key | Decisions | Outcome |")
            lines.append("| --- | --- | --- |")
            for ex in examples[:25]:
                lines.append(
                    f"| `{ex.get('key')}` | "
                    f"`{json.dumps(ex.get('decisions') or {}, sort_keys=True)}` | "
                    f"`{json.dumps(ex.get('outcome') or {}, sort_keys=True)}` |"
                )
            lines.append("")
    return "\n".join(lines) + "\n"


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument(
        "--paper-roots",
        type=str,
        default="",
        help="Comma-separated paper roots, e.g. data/paper_A_current,data/paper_B_cal_only.",
    )
    p.add_argument(
        "--date-range",
        type=str,
        default=":",
        help="Inclusive YYYY-MM-DD:YYYY-MM-DD range. Either side may be blank.",
    )
    p.add_argument("--out", type=Path, default=DEFAULT_OUTPUT_ROOT)
    return p.parse_args(argv)


def _parse_date_range(raw: str) -> Tuple[str, str]:
    if ":" not in raw:
        return raw, raw
    start, end = raw.split(":", 1)
    return start.strip(), end.strip()


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    roots = [Path(p.strip()) for p in args.paper_roots.split(",") if p.strip()]
    if not roots:
        raise SystemExit("--paper-roots is required")
    missing = [str(p) for p in roots if not p.exists()]
    if missing:
        raise SystemExit(f"Missing paper root(s): {', '.join(missing)}")
    start, end = _parse_date_range(args.date_range)
    report = build_report(roots, start, end)

    args.out.mkdir(parents=True, exist_ok=True)
    suffix = f"{start or 'first'}_{end or 'last'}"
    json_path = args.out / f"parallel_engine_comparison_{suffix}.json"
    md_path = args.out / f"parallel_engine_comparison_{suffix}.md"
    json_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    md_path.write_text(render_markdown(report), encoding="utf-8")
    canonical_json = args.out / "parallel_engine_comparison.json"
    canonical_md = args.out / "parallel_engine_comparison.md"
    canonical_json.write_text(json.dumps(report, indent=2), encoding="utf-8")
    canonical_md.write_text(render_markdown(report), encoding="utf-8")
    print(f"Wrote {json_path}")
    print(f"Wrote {md_path}")
    print(f"Wrote {canonical_json}")
    print(f"Wrote {canonical_md}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
