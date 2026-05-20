import json
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple, Sequence

from .constants import PROJECT_DIR, PROMOTION_LAG_SESSION_ROOTS, DRIFT_WILSON_Z

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _load_json(path: Path) -> Dict[str, Any]:
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def _load_jsonl(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    rows: List[Dict[str, Any]] = []
    with path.open(encoding="utf-8") as f:
        for raw in f:
            raw = raw.strip()
            if raw:
                rows.append(json.loads(raw))
    return rows


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        if value is None or value == "":
            return default
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _fmt_money(value: Any) -> str:
    return f"${_safe_float(value):+.2f}"


def _fmt_pct(value: Any) -> str:
    if value is None or value == "":
        return "n/a"
    return f"{_safe_float(value) * 100:.1f}%"


def _line_key(value: Any) -> str:
    try:
        return f"{float(value):.1f}"
    except Exception:
        return str(value or "")


def _latest_session_date(sessions_dir: Path) -> str:
    files = sorted(sessions_dir.glob("*_session.json"))
    if not files:
        raise FileNotFoundError(f"No session files found in {sessions_dir}")
    latest = files[-1].name.replace("_session.json", "")
    if not latest:
        raise FileNotFoundError(f"Could not infer latest session date from {files[-1]}")
    return latest


def _top_counter(mapping: Dict[str, Any], limit: int = 12) -> List[Dict[str, Any]]:
    items = []
    for key, value in (mapping or {}).items():
        items.append({"key": str(key), "count": _safe_int(value)})
    items.sort(key=lambda row: row["count"], reverse=True)
    return items[:limit]


def _empty_side_totals() -> Dict[str, Any]:
    """Per-side counters used by `_summarize_bets`. Mirrors the
    top-level shape so consumers can read either."""
    return {
        "count": 0,
        "filled": 0,
        "wins": 0,
        "losses": 0,
        "profit": 0.0,
        "stake_or_cost": 0.0,
    }


def _finalize_side_totals(t: Dict[str, Any]) -> Dict[str, Any]:
    if t["filled"]:
        t["win_rate"] = t["wins"] / t["filled"]
        t["roi"] = (
            t["profit"] / t["stake_or_cost"] if t["stake_or_cost"] else None
        )
    else:
        t["win_rate"] = None
        t["roi"] = None
    return t


def _summarize_bets(bets: Iterable[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    totals = {
        "count": 0,
        "filled": 0,
        "wins": 0,
        "losses": 0,
        "profit": 0.0,
        "stake_or_cost": 0.0,
        "avg_entry_ask": None,
        "avg_limit_price": None,
        "avg_fair_value": None,
        "avg_current_state_value_edge": None,
        "avg_phantom_risk_score": None,
    }
    by_side: Dict[str, Dict[str, Any]] = {
        "over": _empty_side_totals(),
        "under": _empty_side_totals(),
    }
    entry_asks: List[float] = []
    limits: List[float] = []
    fvs: List[float] = []
    current_edges: List[float] = []
    phantom_scores: List[float] = []

    for bet in bets:
        side = str(bet.get("side") or "over").lower()
        if side not in by_side:
            by_side[side] = _empty_side_totals()
        side_totals = by_side[side]
        status = str(bet.get("order_status") or "")
        is_filled = status == "filled"
        won = bet.get("won")
        profit = _safe_float(bet.get("profit"))
        fill_cost = bet.get("fill_cost_usdc", bet.get("fill_cost"))
        stake_or_cost = _safe_float(fill_cost, _safe_float(bet.get("stake")))
        side_totals["count"] += 1
        if is_filled:
            totals["filled"] += 1
            totals["stake_or_cost"] += stake_or_cost
            totals["profit"] += profit
            side_totals["filled"] += 1
            side_totals["stake_or_cost"] += stake_or_cost
            side_totals["profit"] += profit
            if won is True:
                totals["wins"] += 1
                side_totals["wins"] += 1
            elif won is False:
                totals["losses"] += 1
                side_totals["losses"] += 1

        entry_ask = _safe_float(bet.get("entry_ask"), None)  # type: ignore[arg-type]
        limit_price = _safe_float(bet.get("limit_price"), None)  # type: ignore[arg-type]
        fair_value = _safe_float(bet.get("fair_value"), None)  # type: ignore[arg-type]
        current_edge = bet.get("current_state_value_edge")
        phantom = bet.get("shadow_phantom_risk_score")
        if entry_ask is not None:
            entry_asks.append(entry_ask)
        if limit_price is not None:
            limits.append(limit_price)
        if fair_value is not None:
            fvs.append(fair_value)
        if current_edge is not None:
            current_edges.append(_safe_float(current_edge))
        if phantom is not None:
            phantom_scores.append(_safe_float(phantom))

        rows.append({
            "bet_id": bet.get("bet_id"),
            "side": side,
            "game": f"{bet.get('away_abbrev', '?')}@{bet.get('home_abbrev', '?')}",
            "line": bet.get("line"),
            "inning": bet.get("inning"),
            "status": status,
            "entry_ask": bet.get("entry_ask"),
            "limit_price": bet.get("limit_price"),
            "actual_fill_price": bet.get("actual_fill_price") or bet.get("fill_price"),
            "filled_shares": bet.get("filled_shares", bet.get("fill_size")),
            "fill_cost_usdc": fill_cost,
            "payout_usdc": bet.get("payout_usdc", bet.get("payout")),
            "fair_value": bet.get("fair_value"),
            "edge": bet.get("edge"),
            "current_state_value_edge": current_edge,
            "current_state_value_empirical_edge": bet.get("current_state_value_empirical_edge"),
            "phantom_risk_band": bet.get("shadow_phantom_risk_band"),
            "phantom_risk_score": phantom,
            "won": won,
            "profit": bet.get("profit"),
            "final_total": bet.get("final_total"),
        })

    totals["count"] = len(rows)
    if totals["filled"]:
        totals["win_rate"] = totals["wins"] / totals["filled"]
        totals["roi"] = totals["profit"] / totals["stake_or_cost"] if totals["stake_or_cost"] else None
    else:
        totals["win_rate"] = None
        totals["roi"] = None

    def _avg(values: List[float]) -> Optional[float]:
        return round(sum(values) / len(values), 6) if values else None

    totals["avg_entry_ask"] = _avg(entry_asks)
    totals["avg_limit_price"] = _avg(limits)
    totals["avg_fair_value"] = _avg(fvs)
    totals["avg_current_state_value_edge"] = _avg(current_edges)
    totals["avg_phantom_risk_score"] = _avg(phantom_scores)
    totals["by_side"] = {
        side: _finalize_side_totals(t) for side, t in by_side.items()
    }
    return rows, totals


def _mean(xs: List[float]) -> Optional[float]:
    return sum(xs) / len(xs) if xs else None


def _wilson_upper_bound(
    successes: int, trials: int, z: float = DRIFT_WILSON_Z
) -> Optional[float]:
    if trials <= 0:
        return None
    n = float(trials)
    p_hat = float(successes) / n
    z2 = z * z
    denom = 1.0 + z2 / n
    center = p_hat + z2 / (2.0 * n)
    radius_sq = (p_hat * (1.0 - p_hat) + z2 / (4.0 * n)) / n
    if radius_sq < 0:
        radius_sq = 0.0
    radius = z * (radius_sq ** 0.5)
    return min(1.0, (center + radius) / denom)


def _shift_date(date_str: str, days: int) -> Optional[str]:
    try:
        base = datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError:
        return None
    return (base + timedelta(days=days)).strftime("%Y-%m-%d")


def _artifact_age_days(generated_at_iso: str, today: str) -> Optional[float]:
    try:
        gen = datetime.fromisoformat(str(generated_at_iso).rstrip("Z"))
    except (TypeError, ValueError):
        return None
    try:
        today_dt = datetime.strptime(today, "%Y-%m-%d")
    except ValueError:
        return None
    return round((today_dt - gen.replace(tzinfo=None)).total_seconds() / 86400.0, 2)


def _parse_iso_to_epoch_safe(value: Any) -> Optional[float]:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.rstrip("Z")).replace(
            tzinfo=timezone.utc,
        ).timestamp()
    except (ValueError, TypeError):
        return None


def _latest_session_start_utc(
    project_root: Path = PROJECT_DIR,
    session_roots: Sequence[str] = PROMOTION_LAG_SESSION_ROOTS,
) -> Optional[Tuple[str, float, str]]:
    candidates: List[Tuple[str, float, str, str]] = []
    for root_rel in session_roots:
        root = project_root / root_rel
        if not root.exists():
            continue
        for entry in sorted(root.iterdir()):
            name = entry.name
            if not name.endswith("_session.json"):
                continue
            try:
                with open(entry, encoding="utf-8") as f:
                    session = json.load(f)
            except (OSError, json.JSONDecodeError):
                continue
            bets = session.get("bets") or []
            first_placed = None
            for bet in bets:
                placed = bet.get("placed_at")
                if isinstance(placed, str) and placed:
                    if first_placed is None or placed < first_placed:
                        first_placed = placed
            chosen = first_placed or session.get("generated_at")
            epoch = _parse_iso_to_epoch_safe(chosen)
            if epoch is None:
                continue
            candidates.append((name, epoch, chosen, root_rel))
    if not candidates:
        return None
    candidates.sort(key=lambda x: x[1])
    name, epoch, iso, _root = candidates[-1]
    return name, epoch, iso


def _load_trailing_reviews(
    *, output_root: Path, today: str, days: int, mode: Optional[str]
) -> List[Dict[str, Any]]:
    """Load up to ``days`` prior daily-review JSONs preceding ``today``.

    Filters by ``mode`` so live runs aren't compared against paper runs.
    Returns most-recent first; days without a review are silently skipped.
    """
    out: List[Dict[str, Any]] = []
    for offset in range(1, days + 1):
        prior_date = _shift_date(today, -offset)
        if not prior_date:
            continue
        path = output_root / f"{prior_date}_human_review.json"
        if not path.exists():
            continue
        try:
            payload = _load_json(path)
        except (OSError, json.JSONDecodeError):
            continue
        if mode is not None and payload.get("mode") not in (None, "", mode):
            continue
        out.append(payload)
    return out


def _drift_ask_bucket(value: Any) -> str:
    """Same ask-bucket boundaries used by build_model_maturity_report."""
    try:
        ask = float(value)
    except (TypeError, ValueError):
        return "missing"
    if ask < 0.55:
        return "<0.55"
    if ask < 0.65:
        return "0.55-0.65"
    if ask < 0.75:
        return "0.65-0.75"
    if ask < 0.85:
        return "0.75-0.85"
    return ">=0.85"


def _drift_current_state_edge_bucket(value: Any) -> str:
    try:
        edge = float(value)
    except (TypeError, ValueError):
        return "missing"
    if edge < 0.03:
        return "<0.03"
    if edge < 0.08:
        return "0.03-0.08"
    return ">=0.08"


def _drift_phantom_band_bucket(value: Any) -> str:
    s = str(value or "").strip().lower()
    return s or "missing"


