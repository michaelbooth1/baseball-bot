#!/usr/bin/env python3
"""build_shadow_clv.py -- T1: shadow-CLV / post-signal market-path collector.

The fill-gated CLV report (`build_clv_report.py`) needs a real fill, so it
only ever sees the handful of bets that actually executed (n=149 lifetime).
But during a paper-only / dry-run window there are NO fills -- yet the engine
still emits placed-candidate *book captures*: for every bet the engine would
have placed, `data/<root>/book_captures/<date>/<bet_id>.jsonl` records the
entry context plus a 2-minute, 1-second-resolution forward order-book path
(best_bid / best_ask / mid after the signal). That is a fill-free measurement
of where the market goes right after we'd have bet.

This builder turns those captures into a "shadow CLV" dataset and -- joined to
realized outcomes -- decomposes the **selection-driven residual** the Stage-1
NB replay flagged (the bot bets where the model is most overconfident, and
those win less in market-selected situations). The question it answers:

    When our model says "bet Over at ask X" and we LOSE, did the market
    drift AWAY from us in the next 2 minutes (it re-priced toward the
    loss faster than we did -> "market knew" / adverse selection), or
    did it stay FLAT (the market didn't see it either -> "model wrong" /
    overconfidence)?

That fork tells us whether the residual is closable by a market/selection-
aware lever (adverse selection is real) or only by a better model
(overconfidence). It also seeds Phase E1 (toxic-flow detection).

Population note: book captures fire on PLACED (trade-decision) candidates, not
every evaluated tick -- which is exactly the bet-conditional set the residual
is about. Across the fleet roots this is hundreds of captures/day, accruing
into thousands over a multi-week window.

Outputs (under data/analysis_output/shadow_clv/):
  - shadow_clv_rows.jsonl / .csv   : one row per placed candidate
  - shadow_clv_summary.json / .md  : aggregates + the won/lost x drift 2x2
"""
from __future__ import annotations

import argparse
import csv
import json
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

PROJECT_DIR = Path(__file__).resolve().parents[2]
DATA_DIR = PROJECT_DIR / "data"
DEFAULT_OUTPUT_DIR = DATA_DIR / "analysis_output" / "shadow_clv"
# When several presets evaluate the same token at the same moment, only one
# book capture is written; the others store a `shared_capture_pointer` row
# referencing the canonical capture here (dedup -- avoids capturing the same
# order book 13x). The collector follows the pointer to recover the path.
SHARED_CAPTURE_DIR = DATA_DIR / "polymarket" / "mlb_ou" / "shared_book_captures"

# Forward horizons (seconds after signal) at which we sample the book.
HORIZONS_S: Tuple[int, ...] = (30, 60, 120)
# A mid move smaller than this (probability units) is "flat" -- neither
# favorable nor adverse. 0.01 = 1 cent, one tick on Polymarket.
FLAT_THRESHOLD = 0.01
# Raw-FV bands, aligned with the calibrator-enforce floor question so the
# shadow-CLV read can be cross-referenced against the muting-winners debate.
def _raw_fv_band(raw_fv: Optional[float]) -> str:
    if raw_fv is None:
        return "unknown"
    if raw_fv >= 0.95:
        return ">=0.95"
    if raw_fv >= 0.90:
        return "0.90-0.95"
    return "<0.90"

# Verdict thresholds.
MIN_SETTLED_FOR_VERDICT = 20
ADVERSE_CORR_THRESHOLD = 0.15   # corr(mid_drift, won) this high => drift predicts outcome
MODEL_SIDE_CORR_CEILING = 0.10  # near-zero corr => market is as blind as we are

# Tape (real-trade) layer: disambiguate the adverse-selection finding. A 2-min
# adverse MID drift can be (a) INFORMED flow -- real signed trades hitting our
# side at signal -- or (b) CHASING -- a FLAT tape (no recent trades) where the
# quote just drifts in a thin/illiquid book and we entered into it. The fork
# decides the fix: INFORMED -> market-anchored model / be the maker; CHASING ->
# cheap entry-timing / liquidity-aware execution (no model). Side note for
# OVER bets: net SELLING of the Over token (signed_volume < 0) is flow AGAINST
# us; net buying is flow WITH us.
TAPE_MIN_FOR_SUBVERDICT = 10
TAPE_SUBVERDICT_SHARE = 0.60

# Liquidity-filter validation: bucket realized taker ROI by entry book quality
# (spread / top-of-book depth / trade-recency) into tertiles. A filter is only
# ACTIONABLE if the thin/wide end is materially -EV while the rest stays
# profitable -- otherwise the chasing drift is a benign CLV drag and a filter
# would just bleed volume ("skip flat-tape" is a trap: ~98% of bets are flat).
BOOK_QUALITY_MIN_SETTLED = 60
BOOK_QUALITY_EV_THRESHOLD = 0.05   # worse-end ROI must be <= -5% to be actionable
BOOK_QUALITY_MIN_BUCKET_N = 20

# Roots whose book_captures we never treat as a config arm.
EXCLUDED_ROOT_NAMES = ("paper_trading",)


# --------------------------------------------------------------------------
# IO helpers
# --------------------------------------------------------------------------
def _config_label(root: Path) -> str:
    name = root.name
    if name == "live_trading":
        return "live"
    if name.startswith("paper_"):
        return name[len("paper_"):]
    return name


def _discover_roots(data_dir: Path) -> List[Path]:
    roots: List[Path] = []
    live = data_dir / "live_trading"
    if (live / "book_captures").is_dir():
        roots.append(live)
    for child in sorted(data_dir.glob("paper_*")):
        if child.name in EXCLUDED_ROOT_NAMES:
            continue
        if (child / "book_captures").is_dir():
            roots.append(child)
    return roots


def _iter_capture_files(
    root: Path, *, since: Optional[str], until: Optional[str]
) -> Iterable[Tuple[str, Path]]:
    """Yield (date, path) for each per-candidate book-capture file in `root`
    whose date partition falls within [since, until]."""
    base = root / "book_captures"
    for date_dir in sorted(base.glob("20*-*-*")):
        if not date_dir.is_dir():
            continue
        date = date_dir.name
        if since and date < since:
            continue
        if until and date > until:
            continue
        for f in sorted(date_dir.glob("*.jsonl")):
            yield date, f


def _load_outcome_lookup(roots: List[Path]) -> Dict[Tuple[Any, str], bool]:
    """Union every `<date>_outcomes.jsonl` across roots into
    {(game_pk, line_str): over_hit}. Outcomes are game truth, identical
    across roots, so the union is safe (last write wins on conflict)."""
    lookup: Dict[Tuple[Any, str], bool] = {}
    for root in roots:
        cu = root / "candidate_universe"
        if not cu.is_dir():
            continue
        for f in sorted(cu.glob("*_outcomes.jsonl")):
            try:
                for line in f.read_text(encoding="utf-8").splitlines():
                    if not line.strip():
                        continue
                    o = json.loads(line)
                    ov = o.get("over_hit")
                    if ov is None:
                        continue
                    lookup[(o.get("game_pk"), str(o.get("line")))] = bool(ov)
            except (OSError, json.JSONDecodeError):
                continue
    return lookup


def _load_tape_index(
    roots: List[Path], *, since: Optional[str], until: Optional[str]
) -> Dict[Tuple[str, str], Dict[str, Any]]:
    """Index PLACED tape captures by (config_label, bet_id) -> features.

    Placed (non-`skip_`) tape files carry the SAME bet_id as the book capture
    (e.g. 2026-04-28_823390_8.5_0001), so the join is exact. Skip captures use
    a different suffix and don't correspond to a placed candidate, so they're
    excluded. There are ~1.3k placed tape files total -- cheap to read."""
    idx: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for root in roots:
        label = _config_label(root)
        base = root / "tape_captures"
        if not base.is_dir():
            continue
        for date_dir in sorted(base.glob("20*-*-*")):
            if not date_dir.is_dir():
                continue
            date = date_dir.name
            if since and date < since:
                continue
            if until and date > until:
                continue
            for f in date_dir.glob("*.json"):
                if f.name.startswith("skip_"):
                    continue
                try:
                    d = json.loads(f.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                bid = d.get("bet_id")
                if bid:
                    idx[(label, str(bid))] = d.get("features") or {}
    return idx


def _tape_direction(feat: Optional[Dict[str, Any]]) -> str:
    """Classify the tape at signal for an OVER bet:
      flat_tape       -- no trades in the last 30s (we entered a quiet book)
      informed_against-- recent NET SELLING of the Over token (flow against us)
      informed_with   -- recent NET BUYING (flow with us)
      flow_neutral    -- recent trades, net zero
      flow_undirected -- recent trades but no signed volume recorded
      no_tape         -- no tape capture joined
    """
    if not feat:
        return "no_tape"
    cnt = feat.get("trades_last_30s_count")
    if cnt is None:
        return "no_tape"
    if cnt == 0:
        return "flat_tape"
    sv = feat.get("signed_volume_last_30s")
    if sv is None:
        return "flow_undirected"
    if sv < 0:
        return "informed_against"
    if sv > 0:
        return "informed_with"
    return "flow_neutral"


def _tape_subverdict(n_classified: int, informed: int, flat: int) -> str:
    """Refine ADVERSE_SELECTION into INFORMED vs CHASING from the tape split of
    the adverse-drift losses."""
    if n_classified < TAPE_MIN_FOR_SUBVERDICT:
        return "INSUFFICIENT_TAPE"
    ishare = informed / n_classified
    fshare = flat / n_classified
    if ishare >= TAPE_SUBVERDICT_SHARE:
        return "INFORMED"   # real flow against us -> market-anchored / pivot
    if fshare >= TAPE_SUBVERDICT_SHARE:
        return "CHASING"    # flat tape -> cheap entry-timing / liquidity lever
    return "MIXED"


# --------------------------------------------------------------------------
# Per-capture parsing + metrics
# --------------------------------------------------------------------------
def _mid_from_book(book: Dict[str, Any]) -> Optional[float]:
    bid = book.get("best_bid")
    ask = book.get("best_ask")
    if isinstance(bid, (int, float)) and isinstance(ask, (int, float)):
        return (float(bid) + float(ask)) / 2.0
    m = book.get("mid")
    if isinstance(m, (int, float)):
        return float(m)
    return None


def _snapshot_at(
    snaps: List[Tuple[float, Dict[str, Any]]], target_s: float
) -> Optional[Dict[str, Any]]:
    """Latest snapshot with elapsed_s <= target_s that has a valid mid."""
    chosen: Optional[Dict[str, Any]] = None
    for elapsed, book in snaps:
        if elapsed > target_s:
            break
        if _mid_from_book(book) is not None:
            chosen = book
    return chosen


def _read_capture_rows(
    path: Path,
) -> Tuple[
    Optional[Dict[str, Any]],
    List[Tuple[float, Dict[str, Any]]],
    Optional[Dict[str, Any]],
]:
    """Read a capture file into (signal_row, snapshots, shared_pointer)."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None, [], None
    signal: Optional[Dict[str, Any]] = None
    snaps: List[Tuple[float, Dict[str, Any]]] = []
    pointer: Optional[Dict[str, Any]] = None
    for line in lines:
        if not line.strip():
            continue
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        t = r.get("type")
        if t == "signal":
            signal = r
        elif t == "snapshot":
            book = r.get("book") or {}
            try:
                elapsed = float(r.get("elapsed_s"))
            except (TypeError, ValueError):
                continue
            snaps.append((elapsed, book))
        elif t == "shared_capture_pointer":
            pointer = r
    return signal, snaps, pointer


def _resolve_shared_path(pointer: Dict[str, Any]) -> Optional[Path]:
    """Resolve a shared_capture_pointer to a path under SHARED_CAPTURE_DIR.

    The stored `shared_capture_path` is an absolute machine-specific path, so
    we reconstruct from its trailing `<date>/<id>.jsonl` under the canonical
    dir (portable + test-friendly); fall back to the literal stored path."""
    raw = str(pointer.get("shared_capture_path") or "")
    parts = [p for p in raw.replace("\\", "/").split("/") if p]
    if len(parts) >= 2:
        cand = SHARED_CAPTURE_DIR / parts[-2] / parts[-1]
        if cand.exists():
            return cand
    lit = Path(raw)
    return lit if raw and lit.exists() else None


def parse_capture(
    path: Path,
    *,
    date: str,
    config_label: str,
    outcome_lookup: Dict[Tuple[Any, str], bool],
) -> Optional[Dict[str, Any]]:
    """Parse one per-candidate book-capture file into a metrics row, or None
    if it is unusable (no signal row / no valid forward path).

    Per-candidate files carry the preset-specific signal context (entry_ask,
    FV). The forward book path is either inline (`snapshot` rows) or, when the
    capture was deduplicated across presets, referenced via a
    `shared_capture_pointer` -- in which case we load the snapshots from the
    shared file while keeping THIS file's signal context."""
    signal, snaps, pointer = _read_capture_rows(path)
    if not snaps and pointer is not None:
        shared = _resolve_shared_path(pointer)
        if shared is not None:
            shared_signal, snaps, _ = _read_capture_rows(shared)
            if signal is None:
                signal = shared_signal
    if signal is None or not snaps:
        return None
    snaps.sort(key=lambda kv: kv[0])

    entry_ask = signal.get("entry_ask")
    raw_fv = signal.get("fair_value")
    base_fv = signal.get("base_fair_value")
    if not isinstance(entry_ask, (int, float)):
        return None
    entry_ask = float(entry_ask)

    entry_book = snaps[0][1]
    entry_mid = _mid_from_book(entry_book)
    if entry_mid is None:
        # Fall back to entry_ask as the entry mark (one-sided book at t0).
        entry_mid = entry_ask

    # Entry book quality (for the liquidity-filter validation): how thin/wide
    # was the book the moment we'd have bet?
    eb_bid = entry_book.get("best_bid")
    eb_ask = entry_book.get("best_ask")
    entry_spread = (
        round(float(eb_ask) - float(eb_bid), 4)
        if isinstance(eb_bid, (int, float)) and isinstance(eb_ask, (int, float))
        else None
    )
    bbs = entry_book.get("best_bid_size")
    bas = entry_book.get("best_ask_size")
    entry_top_depth = (
        round(float(bbs or 0.0) + float(bas or 0.0), 2)
        if (bbs is not None or bas is not None) else None
    )

    row: Dict[str, Any] = {
        "bet_id": signal.get("bet_id"),
        "config_label": config_label,
        "session_date": date,
        "entry_ts": signal.get("ts"),
        "game_pk": signal.get("game_pk"),
        "line": str(signal.get("line")),
        "side": str(signal.get("side") or "over").lower(),
        "token_id": signal.get("token_id"),
        "inning": signal.get("inning"),
        "entry_ask": round(entry_ask, 4),
        "fair_value": round(float(raw_fv), 4) if isinstance(raw_fv, (int, float)) else None,
        "base_fair_value": round(float(base_fv), 4) if isinstance(base_fv, (int, float)) else None,
        "raw_fv_band": _raw_fv_band(float(raw_fv) if isinstance(raw_fv, (int, float)) else None),
        "entry_mid": round(entry_mid, 4),
        "entry_spread": entry_spread,
        "entry_top_depth": entry_top_depth,
        "max_elapsed_s": round(snaps[-1][0], 1),
    }

    # Forward path at each horizon.
    last_valid_mid: Optional[float] = None
    for h in HORIZONS_S:
        book = _snapshot_at(snaps, float(h))
        if book is None:
            row[f"mid_{h}s"] = None
            row[f"ask_{h}s"] = None
        else:
            m = _mid_from_book(book)
            row[f"mid_{h}s"] = round(m, 4) if m is not None else None
            ask = book.get("best_ask")
            row[f"ask_{h}s"] = round(float(ask), 4) if isinstance(ask, (int, float)) else None
            if m is not None:
                last_valid_mid = m

    # Drift + shadow-CLV at the longest horizon we actually reached.
    final_mid = row.get("mid_120s")
    if final_mid is None:
        final_mid = last_valid_mid
    if final_mid is not None:
        mid_drift = final_mid - entry_mid
        row["mid_drift_120s"] = round(mid_drift, 4)
        row["shadow_clv_120s"] = round(final_mid - entry_ask, 4)
        if mid_drift >= FLAT_THRESHOLD:
            row["adverse_sign"] = "favorable"
        elif mid_drift <= -FLAT_THRESHOLD:
            row["adverse_sign"] = "adverse"
        else:
            row["adverse_sign"] = "flat"
        row["path_complete"] = True
    else:
        row["mid_drift_120s"] = None
        row["shadow_clv_120s"] = None
        row["adverse_sign"] = "no_path"
        row["path_complete"] = False

    # Outcome join (game truth). side is over by construction of the OVER
    # placement pipeline; won = over_hit.
    won = outcome_lookup.get((row["game_pk"], row["line"]))
    if won is None:
        row["settled"] = False
        row["won"] = None
        row["shadow_clv_vs_settle"] = None
    else:
        row["settled"] = True
        row["won"] = bool(won)
        row["shadow_clv_vs_settle"] = round((1.0 if won else 0.0) - entry_ask, 4)
    return row


# --------------------------------------------------------------------------
# Aggregation
# --------------------------------------------------------------------------
def _corr(xs: List[float], ys: List[float]) -> Optional[float]:
    n = len(xs)
    if n < 3:
        return None
    mx = sum(xs) / n
    my = sum(ys) / n
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    vx = sum((x - mx) ** 2 for x in xs)
    vy = sum((y - my) ** 2 for y in ys)
    if vx <= 0 or vy <= 0:
        return None
    return cov / math.sqrt(vx * vy)


def _mean(xs: List[float]) -> Optional[float]:
    return sum(xs) / len(xs) if xs else None


def _dedup_key(r: Dict[str, Any]) -> Tuple[Any, Any, Any, str]:
    return (
        r.get("game_pk"), r.get("line"), r.get("token_id"),
        str(r.get("entry_ts"))[:19],
    )


def _taker_profit(row: Dict[str, Any]) -> Optional[float]:
    """Realized taker ROI per $1 cost for an OVER bet entered at entry_ask:
    (1-a)/a on a win, -1 on a loss. None if unsettled / no ask."""
    if not row.get("settled"):
        return None
    a = row.get("entry_ask")
    if not isinstance(a, (int, float)) or a <= 0:
        return None
    return (1.0 - a) / a if row.get("won") else -1.0


def _book_quality_tertiles(
    rows: List[Dict[str, Any]], key: str, *, higher_is_worse: bool, label: str,
) -> Optional[Dict[str, Any]]:
    """Bucket settled rows into low/mid/high tertiles of `key` and report
    cohort taker-ROI / win-rate / n per bucket, marking the 'worse' (thin/wide)
    end."""
    pairs = [
        (r[key], _taker_profit(r), bool(r.get("won")))
        for r in rows
        if isinstance(r.get(key), (int, float)) and _taker_profit(r) is not None
    ]
    if len(pairs) < 30:
        return None
    sv = sorted(p[0] for p in pairs)
    q1 = sv[len(sv) // 3]
    q2 = sv[2 * len(sv) // 3]
    bk: Dict[str, List[Tuple[float, bool]]] = {"low": [], "mid": [], "high": []}
    for v, prof, won in pairs:
        b = "low" if v < q1 else ("mid" if v < q2 else "high")
        bk[b].append((prof, won))

    def _stats(items: List[Tuple[float, bool]]) -> Dict[str, Any]:
        n = len(items)
        return {
            "n": n,
            "roi": round(sum(p for p, _ in items) / n, 4) if n else None,
            "win_rate": round(sum(1 for _, w in items if w) / n, 4) if n else None,
        }

    worse = "high" if higher_is_worse else "low"
    better = "low" if higher_is_worse else "high"
    out = {
        "metric": label,
        "tertile_cuts": [round(q1, 4), round(q2, 4)],
        "higher_is_worse": higher_is_worse,
        "worse_end": worse,
        "buckets": {b: _stats(bk[b]) for b in ("low", "mid", "high")},
    }
    wr, br = out["buckets"][worse]["roi"], out["buckets"][better]["roi"]
    out["worse_minus_better_roi"] = (
        round(wr - br, 4) if (wr is not None and br is not None) else None
    )
    return out


def _book_quality_verdict(
    bq: Dict[str, Any], n_settled: int,
) -> Dict[str, Any]:
    """ACTIONABLE_FILTER if any dimension splits into a clearly +EV 'good' end
    and a materially -EV remainder (filter to keep the good end). Compares the
    good-end tertile against the OTHER TWO combined, so it catches the real
    shape -- e.g. only deep books are +EV while the bottom 2/3 by depth lose --
    not just worst-tertile-vs-best. Otherwise BENIGN_DRAG: the chasing drift
    doesn't separate a losing cohort, so a filter would just bleed volume."""
    if n_settled < BOOK_QUALITY_MIN_SETTLED:
        return {"verdict": "INSUFFICIENT_DATA", "actionable_dimensions": []}
    actionable: List[Dict[str, Any]] = []
    for dim, res in bq.items():
        if not res:
            continue
        worse = res["worse_end"]
        good = "low" if worse == "high" else "high"
        b = res["buckets"]
        good_roi = b[good]["roi"]
        good_n = b[good]["n"]
        rest_keys = [k for k in ("low", "mid", "high") if k != good]
        rest_n = sum(b[k]["n"] for k in rest_keys)
        rest_roi = (
            sum(b[k]["n"] * b[k]["roi"] for k in rest_keys if b[k]["roi"] is not None)
            / rest_n
        ) if rest_n else None
        if good_roi is None or rest_roi is None:
            continue
        if (
            good_roi >= BOOK_QUALITY_EV_THRESHOLD
            and rest_roi <= -BOOK_QUALITY_EV_THRESHOLD
            and good_n >= BOOK_QUALITY_MIN_BUCKET_N
            and rest_n >= BOOK_QUALITY_MIN_BUCKET_N
        ):
            cut = res["tertile_cuts"][1] if good == "high" else res["tertile_cuts"][0]
            actionable.append({
                "dimension": dim,
                "keep_end": good,
                "threshold": cut,  # keep good>=cut (high) or good<cut (low)
                "keep_roi": good_roi,
                "filtered_roi": round(rest_roi, 4),
                "keep_n": good_n,
                "filtered_n": rest_n,
            })
    return {
        "verdict": "ACTIONABLE_FILTER" if actionable else "BENIGN_DRAG",
        "actionable_dimensions": actionable,
    }


def aggregate(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    with_path_all = [r for r in rows if r.get("path_complete")]
    # Pseudo-replication guard: presets share one underlying book capture
    # (shared_capture_pointer), so the SAME post-signal market path + outcome
    # appears once per preset. The adverse-selection stats must count each
    # distinct path ONCE, else corr / shares are computed on ~13 correlated
    # copies. Identity = (game_pk, line, token_id, entry_ts to the second);
    # prefer the live-root row when several presets share a path.
    _seen: Dict[Tuple[Any, Any, Any, str], Dict[str, Any]] = {}
    for r in sorted(
        with_path_all, key=lambda r: 0 if r.get("config_label") == "live" else 1
    ):
        _seen.setdefault(_dedup_key(r), r)
    with_path = list(_seen.values())
    settled = [r for r in with_path if r.get("settled")]

    drifts = [r["mid_drift_120s"] for r in with_path]
    summary: Dict[str, Any] = {
        "n_candidate_rows": len(rows),
        "n_candidate_rows_with_path": len(with_path_all),
        "n_candidates": len(with_path),       # unique market paths (deduped)
        "n_unique_paths": len(with_path),
        "n_with_path": len(with_path),
        "n_settled_with_path": len(settled),
        "mean_mid_drift_120s": _mean(drifts),
        "mean_shadow_clv_120s": _mean([r["shadow_clv_120s"] for r in with_path]),
        "favorable_share": (
            sum(1 for r in with_path if r["adverse_sign"] == "favorable") / len(with_path)
            if with_path else None
        ),
        "adverse_share": (
            sum(1 for r in with_path if r["adverse_sign"] == "adverse") / len(with_path)
            if with_path else None
        ),
        "flat_share": (
            sum(1 for r in with_path if r["adverse_sign"] == "flat") / len(with_path)
            if with_path else None
        ),
    }

    # The headline decomposition: won/lost x drift sign, on settled bets.
    cells = {
        (w, s): 0
        for w in ("won", "lost")
        for s in ("favorable", "adverse", "flat")
    }
    for r in settled:
        w = "won" if r["won"] else "lost"
        cells[(w, r["adverse_sign"])] += 1
    losses = sum(cells[("lost", s)] for s in ("favorable", "adverse", "flat"))
    summary["decomposition_2x2"] = {
        f"{w}_{s}": cells[(w, s)]
        for w in ("won", "lost")
        for s in ("favorable", "adverse", "flat")
    }
    summary["n_losses_settled"] = losses
    summary["market_knew_share"] = (
        cells[("lost", "adverse")] / losses if losses else None
    )  # losses where market drifted away from us = it re-priced toward the loss
    summary["model_wrong_share"] = (
        cells[("lost", "flat")] / losses if losses else None
    )  # losses where market stayed flat = we were overconfident, not selected against

    # Does the 2-min drift predict the outcome?
    settle_drifts = [r["mid_drift_120s"] for r in settled]
    settle_wins = [1.0 if r["won"] else 0.0 for r in settled]
    summary["corr_drift_vs_win"] = _corr(settle_drifts, settle_wins)
    summary["mean_loss_drift_120s"] = _mean(
        [r["mid_drift_120s"] for r in settled if not r["won"]]
    )

    # By raw-FV band (does the overconfident high-FV tail drift adverse?).
    band_stats: Dict[str, Dict[str, Any]] = {}
    for band in (">=0.95", "0.90-0.95", "<0.90", "unknown"):
        b = [r for r in settled if r["raw_fv_band"] == band]
        if not b:
            continue
        b_loss = [r for r in b if not r["won"]]
        band_stats[band] = {
            "n_settled": len(b),
            "mean_mid_drift_120s": _mean([r["mid_drift_120s"] for r in b]),
            "win_rate": _mean([1.0 if r["won"] else 0.0 for r in b]),
            "mean_loss_drift_120s": _mean([r["mid_drift_120s"] for r in b_loss]),
        }
    summary["by_raw_fv_band"] = band_stats

    # By config arm (so presets can be compared later). Uses the FULL
    # per-preset rows (not the deduped set) -- each arm's own decisions.
    by_label: Dict[str, Dict[str, Any]] = {}
    for r in with_path_all:
        lab = r["config_label"]
        d = by_label.setdefault(lab, {"n": 0, "drifts": [], "n_settled": 0})
        d["n"] += 1
        d["drifts"].append(r["mid_drift_120s"])
        if r.get("settled"):
            d["n_settled"] += 1
    summary["by_config_label"] = {
        lab: {
            "n_with_path": d["n"],
            "n_settled": d["n_settled"],
            "mean_mid_drift_120s": _mean(d["drifts"]),
        }
        for lab, d in sorted(by_label.items())
    }

    summary["verdict"] = _verdict(summary)

    # Tape decomposition: is the adverse drift INFORMED (real flow against us)
    # or CHASING (flat tape -- thin book, quote-only drift)? Computed on the
    # adverse-drift settled LOSSES (the market-knew cohort), plus a
    # population-level flat-tape share for context.
    def _dir_counts(items: List[Dict[str, Any]]) -> Dict[str, int]:
        out: Dict[str, int] = {}
        for r in items:
            out[r.get("tape_direction", "no_tape")] = (
                out.get(r.get("tape_direction", "no_tape"), 0) + 1
            )
        return out

    adv_losses = [
        r for r in settled
        if (r.get("won") is False) and r.get("adverse_sign") == "adverse"
    ]
    classified = [
        r for r in adv_losses
        if r.get("tape_direction") not in (None, "no_tape")
    ]
    n_cls = len(classified)
    informed = sum(1 for r in classified if r["tape_direction"] == "informed_against")
    flat = sum(1 for r in classified if r["tape_direction"] == "flat_tape")
    with_tape_all = [r for r in with_path if r.get("tape_direction") not in (None, "no_tape")]
    flat_all = sum(1 for r in with_tape_all if r["tape_direction"] == "flat_tape")
    summary["tape_decomposition"] = {
        "n_paths_with_tape": len(with_tape_all),
        "population_flat_tape_share": (
            flat_all / len(with_tape_all) if with_tape_all else None
        ),
        "n_adverse_losses": len(adv_losses),
        "n_adverse_losses_with_tape": n_cls,
        "adverse_loss_by_tape_direction": _dir_counts(adv_losses),
        "informed_against": informed,
        "flat_tape": flat,
        "informed_share": (informed / n_cls) if n_cls else None,
        "flat_share": (flat / n_cls) if n_cls else None,
    }
    summary["tape_subverdict"] = _tape_subverdict(n_cls, informed, flat)

    # Liquidity-filter validation: does entry book quality separate a -EV
    # sub-cohort (-> filter threshold) or is the chasing drift a benign drag?
    n_settled_roi = sum(1 for r in settled if _taker_profit(r) is not None)
    summary["overall_taker_roi"] = (
        round(
            sum(_taker_profit(r) for r in settled if _taker_profit(r) is not None)
            / n_settled_roi, 4,
        ) if n_settled_roi else None
    )
    bq = {
        "spread": _book_quality_tertiles(
            settled, "entry_spread", higher_is_worse=True, label="entry_spread"),
        "top_depth": _book_quality_tertiles(
            settled, "entry_top_depth", higher_is_worse=False, label="entry_top_depth"),
        "seconds_since_trade": _book_quality_tertiles(
            settled, "tape_seconds_since_trade", higher_is_worse=True,
            label="seconds_since_last_trade"),
    }
    summary["by_book_quality"] = bq
    summary["book_quality_verdict"] = _book_quality_verdict(bq, n_settled_roi)

    summary["thresholds"] = {
        "flat_threshold": FLAT_THRESHOLD,
        "horizons_s": list(HORIZONS_S),
        "min_settled_for_verdict": MIN_SETTLED_FOR_VERDICT,
        "adverse_corr_threshold": ADVERSE_CORR_THRESHOLD,
        "model_side_corr_ceiling": MODEL_SIDE_CORR_CEILING,
    }
    return summary


def _verdict(summary: Dict[str, Any]) -> str:
    n = summary["n_settled_with_path"]
    if n < MIN_SETTLED_FOR_VERDICT:
        return "COLLECTING"
    corr = summary.get("corr_drift_vs_win")
    loss_drift = summary.get("mean_loss_drift_120s")
    if corr is None or loss_drift is None:
        return "COLLECTING"
    adverse = corr >= ADVERSE_CORR_THRESHOLD and loss_drift <= -FLAT_THRESHOLD
    model_side = abs(corr) < MODEL_SIDE_CORR_CEILING and loss_drift > -FLAT_THRESHOLD
    if adverse and not model_side:
        return "ADVERSE_SELECTION"
    if model_side and not adverse:
        return "MODEL_SIDE"
    return "MIXED"


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------
def _pct(x: Optional[float]) -> str:
    return f"{x * 100:.1f}%" if isinstance(x, (int, float)) else "n/a"


def _cents(x: Optional[float]) -> str:
    return f"{x * 100:+.2f}c" if isinstance(x, (int, float)) else "n/a"


def render_markdown(summary: Dict[str, Any], generated_at: str) -> str:
    L: List[str] = []
    L.append("# Shadow-CLV / Post-Signal Market-Path Report")
    L.append("")
    L.append(f"Generated: {generated_at}")
    L.append("")
    L.append(
        f"Verdict: **{summary['verdict']}** "
        f"({summary['n_candidates']} placed candidates, "
        f"{summary['n_with_path']} with a 2-min path, "
        f"{summary['n_settled_with_path']} settled)"
    )
    L.append("")
    L.append("## Forward market path (0 -> 120s after signal)")
    L.append(
        f"- Mean mid drift: **{_cents(summary['mean_mid_drift_120s'])}**; "
        f"favorable {_pct(summary['favorable_share'])} / "
        f"adverse {_pct(summary['adverse_share'])} / "
        f"flat {_pct(summary['flat_share'])}"
    )
    L.append(f"- Mean shadow-CLV vs entry ask: {_cents(summary['mean_shadow_clv_120s'])}")
    L.append(
        f"- corr(mid drift, win): "
        f"{summary['corr_drift_vs_win'] if summary['corr_drift_vs_win'] is not None else 'n/a'}; "
        f"mean drift on LOSSES: {_cents(summary['mean_loss_drift_120s'])}"
    )
    L.append("")
    L.append("## Selection decomposition (market-knew vs model-wrong)")
    L.append(
        f"Of {summary['n_losses_settled']} settled losses: "
        f"**{_pct(summary['market_knew_share'])} drifted adverse** "
        f"(market re-priced away from us = market-knew) vs "
        f"**{_pct(summary['model_wrong_share'])} flat** "
        f"(market stayed = model-wrong)."
    )
    L.append("")
    L.append("| outcome | favorable | adverse | flat |")
    L.append("|---|---:|---:|---:|")
    d = summary["decomposition_2x2"]
    for w in ("won", "lost"):
        L.append(
            f"| {w} | {d[f'{w}_favorable']} | {d[f'{w}_adverse']} | {d[f'{w}_flat']} |"
        )
    L.append("")
    L.append("## Tape decomposition (informed flow vs chasing)")
    td = summary.get("tape_decomposition") or {}
    L.append(
        f"**Tape subverdict: {summary.get('tape_subverdict', 'n/a')}** — of the "
        f"{td.get('n_adverse_losses_with_tape', 0)} adverse-drift losses with a "
        f"tape capture, **{_pct(td.get('informed_share'))} INFORMED** (real net "
        f"selling against us = market-knew) vs **{_pct(td.get('flat_share'))} "
        f"CHASING** (flat tape — thin book, quote-only drift). "
        f"Population flat-tape share at signal: "
        f"**{_pct(td.get('population_flat_tape_share'))}** "
        f"(n={td.get('n_paths_with_tape', 0)})."
    )
    L.append(
        "_INFORMED → market-anchored model / be the maker; "
        "CHASING → cheap entry-timing / liquidity-aware execution (no model)._"
    )
    L.append("")
    L.append(f"- adverse-loss tape directions: {td.get('adverse_loss_by_tape_direction')}")
    L.append("")
    L.append("## Liquidity filter validation (taker ROI by entry book quality)")
    bv = summary.get("book_quality_verdict") or {}
    L.append(
        f"**Verdict: {bv.get('verdict', 'n/a')}** "
        f"(overall taker ROI {_pct(summary.get('overall_taker_roi'))} on "
        f"{summary.get('n_settled_with_path', 0)} settled). 'Skip flat-tape' is a "
        "trap (~all bets are flat); the question is whether a continuous "
        "book-quality metric separates a -EV cohort."
    )
    for d in bv.get("actionable_dimensions") or []:
        L.append(
            f"- ✅ **{d['dimension']}**: keep `{d['keep_end']}` end at cut "
            f"{d['threshold']} -> kept ROI {_pct(d['keep_roi'])} (n={d['keep_n']}) "
            f"vs filtered {_pct(d['filtered_roi'])} (n={d['filtered_n']})."
        )
    L.append("")
    L.append("| dimension | tertile | n | win rate | taker ROI |")
    L.append("|---|---|---:|---:|---:|")
    for dim, res in (summary.get("by_book_quality") or {}).items():
        if not res:
            L.append(f"| {dim} | (insufficient) | | | |")
            continue
        for t in ("low", "mid", "high"):
            bb = res["buckets"][t]
            star = " *(worse)*" if t == res["worse_end"] else ""
            L.append(
                f"| {dim} | {t}{star} | {bb['n']} | {_pct(bb['win_rate'])} | "
                f"{_pct(bb['roi'])} |"
            )
    L.append("")
    L.append("## By raw-FV band")
    L.append("| band | n | win rate | mean drift | mean loss drift |")
    L.append("|---|---:|---:|---:|---:|")
    for band, s in summary["by_raw_fv_band"].items():
        L.append(
            f"| {band} | {s['n_settled']} | {_pct(s['win_rate'])} | "
            f"{_cents(s['mean_mid_drift_120s'])} | {_cents(s['mean_loss_drift_120s'])} |"
        )
    L.append("")
    L.append("## By config arm")
    L.append("| arm | n path | n settled | mean drift |")
    L.append("|---|---:|---:|---:|")
    for lab, s in summary["by_config_label"].items():
        L.append(
            f"| {lab} | {s['n_with_path']} | {s['n_settled']} | "
            f"{_cents(s['mean_mid_drift_120s'])} |"
        )
    L.append("")
    return "\n".join(L)


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------
def build(
    *,
    data_dir: Path = DATA_DIR,
    since: Optional[str] = None,
    until: Optional[str] = None,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    roots = _discover_roots(data_dir)
    outcome_lookup = _load_outcome_lookup(roots)
    rows: List[Dict[str, Any]] = []
    for root in roots:
        label = _config_label(root)
        for date, path in _iter_capture_files(root, since=since, until=until):
            r = parse_capture(
                path, date=date, config_label=label, outcome_lookup=outcome_lookup
            )
            if r is not None:
                rows.append(r)

    # Tape layer: join each placed candidate to its real-trade capture by
    # (config_label, bet_id) and classify the tape direction at signal.
    tape_index = _load_tape_index(roots, since=since, until=until)
    for r in rows:
        feat = tape_index.get((r.get("config_label"), str(r.get("bet_id"))))
        r["tape_trades_30s"] = feat.get("trades_last_30s_count") if feat else None
        r["tape_signed_vol_30s"] = feat.get("signed_volume_last_30s") if feat else None
        r["tape_ltp_minus_ask"] = feat.get("ltp_minus_ask_last_3_trades") if feat else None
        r["tape_seconds_since_trade"] = feat.get("seconds_since_last_trade") if feat else None
        r["tape_direction"] = _tape_direction(feat)

    summary = aggregate(rows)
    return rows, summary


def _default_since(trailing_days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=trailing_days)).strftime(
        "%Y-%m-%d"
    )


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    p.add_argument("--data-dir", type=Path, default=DATA_DIR)
    p.add_argument(
        "--trailing-days", type=int, default=45,
        help="Only read book-capture date partitions within this many days "
        "(default 45). Use --all to disable.",
    )
    p.add_argument("--since", default=None, help="YYYY-MM-DD lower bound (overrides --trailing-days).")
    p.add_argument("--until", default=None, help="YYYY-MM-DD upper bound.")
    p.add_argument("--all", action="store_true", help="Read all available dates.")
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    since = args.since
    if since is None and not args.all:
        since = _default_since(args.trailing_days)
    rows, summary = build(data_dir=args.data_dir, since=since, until=args.until)

    generated_at = datetime.now(timezone.utc).isoformat()
    out = args.output_dir
    out.mkdir(parents=True, exist_ok=True)

    (out / "shadow_clv_rows.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8"
    )
    if rows:
        fieldnames = list(rows[0].keys())
        # Union keys defensively (rows can differ on optional fields).
        for r in rows:
            for k in r:
                if k not in fieldnames:
                    fieldnames.append(k)
        with (out / "shadow_clv_rows.csv").open("w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=fieldnames)
            w.writeheader()
            w.writerows(rows)
    payload = dict(summary)
    payload["generated_at_utc"] = generated_at
    payload["since"] = since
    payload["until"] = args.until
    (out / "shadow_clv_summary.json").write_text(
        json.dumps(payload, indent=2), encoding="utf-8"
    )
    (out / "shadow_clv_summary.md").write_text(
        render_markdown(summary, generated_at), encoding="utf-8"
    )
    print(
        f"shadow_clv: {summary['n_candidates']} candidates, "
        f"{summary['n_settled_with_path']} settled, verdict={summary['verdict']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
