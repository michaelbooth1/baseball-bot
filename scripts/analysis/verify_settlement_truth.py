#!/usr/bin/env python3
"""verify_settlement_truth.py -- cross-check settled bets against MLB ground truth.

Active priority #12 (2026-05-17). For every bet recorded as filled
in a session JSON, look up the corresponding MLB live-feed JSON and
verify:

  1. The reported `won` field matches the MLB-derived expected
     resolution (over_won = final_total > line).
  2. The bet's `final_total` matches the MLB linescore total
     (home.runs + away.runs).
  3. The bet's status is consistent with the game state (no
     bet-settled-before-game-final ordering bugs, no
     filled-but-never-settled stale fills).

The reason this matters NOW: Phase C C2 inventory tracker just
exposed that ~69 games show "open" inventory in
live_orders_ledger.jsonl even though many are actually settled. When
Phase C v2 actuates two-sided quotes against that inventory, the
stale-settlement class becomes wrong quotes + wrong inventory limits.
This builder bounds that drift by computing a daily summary of
ground-truth disagreements.

Verification result codes (per-bet):
  - `ok` -- everything matches
  - `resolution_mismatch` -- won != expected_won; ROI math corrupted
  - `total_mismatch` -- bet.final_total != mlb_total but won agrees
    (less severe; both calls happened to land on the same side of
    the line but engine recorded the wrong total -- diagnostic)
  - `stale_filled` -- order_status=filled, won is None, MLB game IS
    final. The settlement event never reached the bet record.
  - `missing_mlb_data` -- bet exists but local MLB JSON missing.
    Likely a data-refresh gap (game folder didn't scrape).
  - `game_not_final_yet` -- bet is settled but MLB says game is
    not yet final. Possible data-ordering bug or recent settlement
    that arrived before next MLB poll.
  - `not_yet_settled` -- bet has order_status=filled but
    settled=False AND game is also not yet final per MLB. This is
    the EXPECTED in-progress state (skipped from alerts, but
    counted for visibility).
  - `not_filled` -- non-fill rows (cancelled, error, dry_run) --
    skipped from totals; tracked separately.

Outputs:
  data/analysis_output/settlement_truth/settlement_truth_report.json
  data/analysis_output/settlement_truth/settlement_truth_report.md
"""

from __future__ import annotations

import argparse
import json
import logging
import math
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional


PROJECT_DIR = Path(__file__).resolve().parents[2]
DEFAULT_SESSIONS_DIR = PROJECT_DIR / "data" / "live_trading" / "sessions"
DEFAULT_PAPER_SESSIONS_DIR = PROJECT_DIR / "data" / "paper_trading" / "sessions"
DEFAULT_GAMES_ROOT = PROJECT_DIR / "data" / "games" / "regular"
DEFAULT_OUTPUT_ROOT = (
    PROJECT_DIR / "data" / "analysis_output" / "settlement_truth"
)

# Severity thresholds used by the daily-review block to decide when
# to fire alerts. Conservative: a single resolution mismatch fires
# critical, because corrupted ROI math poisons everything downstream.
STALE_FILLED_ALERT_THRESHOLD = 1
MISSING_MLB_DATA_RATE_ALERT_THRESHOLD = 0.10
STALE_OLDEST_DAYS_ALERT_THRESHOLD = 7

LOGGER = logging.getLogger("verify_settlement_truth")


@dataclass
class GameFinalState:
    """MLB ground truth for one game. Parsed from the live-feed JSON."""
    game_pk: int
    is_final: bool
    total_runs: Optional[int]
    home_runs: Optional[int]
    away_runs: Optional[int]
    status_label: str
    source_path: Optional[str]


@dataclass
class BetVerification:
    """Per-bet verification record. Stored in the JSON output for
    operator inspection. `result_code` is the canonical taxonomy
    enumerated in the module docstring."""
    bet_id: str
    session_date: str
    game_pk: int
    line: Optional[float]
    side: str
    order_status: str
    settled: Optional[bool]
    engine_won: Optional[bool]
    engine_final_total: Optional[int]
    engine_profit: Optional[float]
    mlb_is_final: bool
    mlb_total_runs: Optional[int]
    mlb_home_runs: Optional[int]
    mlb_away_runs: Optional[int]
    expected_won: Optional[bool]
    result_code: str
    notes: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "bet_id": self.bet_id,
            "session_date": self.session_date,
            "game_pk": self.game_pk,
            "line": self.line,
            "side": self.side,
            "order_status": self.order_status,
            "settled": self.settled,
            "engine_won": self.engine_won,
            "engine_final_total": self.engine_final_total,
            "engine_profit": self.engine_profit,
            "mlb_is_final": self.mlb_is_final,
            "mlb_total_runs": self.mlb_total_runs,
            "mlb_home_runs": self.mlb_home_runs,
            "mlb_away_runs": self.mlb_away_runs,
            "expected_won": self.expected_won,
            "result_code": self.result_code,
            "notes": self.notes,
        }


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _safe_int(v: Any) -> Optional[int]:
    if v is None or v == "":
        return None
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


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


def game_json_path(games_root: Path, game_pk: int, session_date: str) -> Path:
    """Compute the canonical MLB game-JSON path for one game on the
    session's date. The path layout is `<year>/<MM>/<DD>/<game_pk>.json`.
    Session date is authoritative for game-date lookup (rescheduled
    games' files live under their actual game date, but the session
    that recorded them used the session date)."""
    parts = session_date.split("-")
    if len(parts) != 3:
        return games_root / "missing" / f"{game_pk}.json"
    year, month, day = parts
    return games_root / year / month / day / f"{game_pk}.json"


def load_game_final_state(
    games_root: Path, game_pk: int, session_date: str,
) -> GameFinalState:
    """Read the MLB live-feed JSON and extract final-state fields.

    Returns a GameFinalState with `is_final=False` and total/runs as
    None when the file is missing -- the caller (verifier) handles
    that as `missing_mlb_data`.
    """
    path = game_json_path(games_root, game_pk, session_date)
    if not path.exists():
        return GameFinalState(
            game_pk=game_pk, is_final=False,
            total_runs=None, home_runs=None, away_runs=None,
            status_label="missing", source_path=None,
        )
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return GameFinalState(
            game_pk=game_pk, is_final=False,
            total_runs=None, home_runs=None, away_runs=None,
            status_label="unreadable", source_path=str(path),
        )
    status_block = (payload.get("gameData") or {}).get("status") or {}
    abstract = str(status_block.get("abstractGameState") or "").strip()
    detailed = str(status_block.get("detailedState") or "").strip()
    is_final = abstract.lower() == "final" or detailed.lower() in {
        "final", "completed early", "game over",
    }
    linescore = (payload.get("liveData") or {}).get("linescore") or {}
    teams = linescore.get("teams") or {}
    home_runs = _safe_int((teams.get("home") or {}).get("runs"))
    away_runs = _safe_int((teams.get("away") or {}).get("runs"))
    total_runs = (
        home_runs + away_runs
        if home_runs is not None and away_runs is not None else None
    )
    return GameFinalState(
        game_pk=game_pk, is_final=is_final,
        total_runs=total_runs, home_runs=home_runs, away_runs=away_runs,
        status_label=detailed or abstract or "unknown",
        source_path=str(path),
    )


def expected_won_for_bet(
    *, side: str, line: Optional[float], total: Optional[int],
) -> Optional[bool]:
    """Derive whether the bet WOULD have won given MLB's final total.

    Returns None if line or total is missing. Polymarket OU lines are
    always `.5` (no pushes possible), but for defensiveness, treat
    total == line as the OVER side losing (would never happen on .5
    lines, but if a future integer line appears, the conservative
    choice is consistent with how the engine records it elsewhere)."""
    if line is None or total is None:
        return None
    if side == "under":
        return total < line
    return total > line


def _bet_line_float(bet: Dict[str, Any]) -> Optional[float]:
    """Parse the bet line value defensively. Session bets store line
    as a string like '8.5'."""
    raw = bet.get("line")
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def verify_bet(
    bet: Dict[str, Any], game_state: GameFinalState,
    *, session_date: str,
) -> BetVerification:
    """Compute the verification record for one bet against the game's
    final state. Pure function; no I/O.

    The result_code taxonomy is in the module docstring.
    """
    line = _bet_line_float(bet)
    side = str(bet.get("side") or "over").lower()
    order_status = str(bet.get("order_status") or "").lower()
    settled = bet.get("settled")
    engine_won = bet.get("won")
    engine_total = _safe_int(bet.get("final_total"))
    engine_profit = _safe_float(bet.get("profit"))

    expected_won = expected_won_for_bet(
        side=side, line=line, total=game_state.total_runs,
    )

    record = BetVerification(
        bet_id=str(bet.get("bet_id") or ""),
        session_date=session_date,
        game_pk=int(bet.get("game_pk") or 0),
        line=line,
        side=side,
        order_status=order_status,
        settled=settled if settled is None else bool(settled),
        engine_won=(
            None if engine_won is None else bool(engine_won)
        ),
        engine_final_total=engine_total,
        engine_profit=engine_profit,
        mlb_is_final=game_state.is_final,
        mlb_total_runs=game_state.total_runs,
        mlb_home_runs=game_state.home_runs,
        mlb_away_runs=game_state.away_runs,
        expected_won=expected_won,
        result_code="ok",
    )

    # ---- classification ladder ----
    # Non-fills (cancelled/error/dry_run) are not in scope for
    # settlement verification.
    if order_status not in {"filled", "settled"}:
        record.result_code = "not_filled"
        record.notes = f"order_status={order_status!r}; not a filled bet"
        return record

    if game_state.status_label == "missing":
        record.result_code = "missing_mlb_data"
        record.notes = "MLB game JSON not found at expected path"
        return record
    if game_state.status_label == "unreadable":
        record.result_code = "missing_mlb_data"
        record.notes = "MLB game JSON exists but failed to parse"
        return record

    if not game_state.is_final:
        # Game in progress. Two cases:
        #   - bet not yet settled -> expected; not an error
        #   - bet IS settled -> ordering bug
        if bool(settled) and engine_won is not None:
            record.result_code = "game_not_final_yet"
            record.notes = (
                f"bet settled (won={engine_won}) but MLB status="
                f"{game_state.status_label!r}"
            )
            return record
        record.result_code = "not_yet_settled"
        record.notes = (
            f"in-progress; MLB status={game_state.status_label!r}"
        )
        return record

    # ---- game IS final from here on ----
    if engine_won is None:
        # Filled bet, game finished, but no won/loss recorded ->
        # settlement event never reached the bet record.
        record.result_code = "stale_filled"
        record.notes = (
            f"order_status=filled, won=None, MLB final "
            f"({game_state.away_runs}-{game_state.home_runs})"
        )
        return record

    if expected_won is None:
        # Should not happen at this branch (game final + line set)
        # but handle defensively.
        record.result_code = "missing_mlb_data"
        record.notes = "could not derive expected_won despite final game"
        return record

    if engine_won != expected_won:
        record.result_code = "resolution_mismatch"
        record.notes = (
            f"engine_won={engine_won} != expected_won={expected_won}; "
            f"MLB total={game_state.total_runs} vs line={line}"
        )
        return record

    if engine_total is not None and engine_total != game_state.total_runs:
        # Same side of the line but engine recorded a different total.
        # Less severe (ROI math is right; total field is the bug).
        record.result_code = "total_mismatch"
        record.notes = (
            f"engine_total={engine_total} != mlb_total="
            f"{game_state.total_runs}"
        )
        return record

    record.result_code = "ok"
    return record


def load_session_bets(
    session_path: Path,
) -> List[Dict[str, Any]]:
    """Read a single session JSON and return its `bets` array."""
    if not session_path.exists():
        return []
    try:
        payload = json.loads(session_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        LOGGER.warning("Failed to read session %s: %s", session_path, exc)
        return []
    return list(payload.get("bets") or [])


def _session_date_from_path(path: Path) -> str:
    """Session files are named `<YYYY-MM-DD>_session.json`."""
    return path.name[:10]


def iterate_sessions(
    sessions_dir: Path, *, min_date: str = "", max_date: str = "",
) -> Iterable[Path]:
    if not sessions_dir.exists():
        return []
    out: List[Path] = []
    for path in sorted(sessions_dir.glob("*_session.json")):
        d = _session_date_from_path(path)
        if min_date and d < min_date:
            continue
        if max_date and d > max_date:
            continue
        out.append(path)
    return out


def days_between(today: str, session_date: str) -> Optional[int]:
    try:
        return (
            date.fromisoformat(today) - date.fromisoformat(session_date)
        ).days
    except (TypeError, ValueError):
        return None


def build_report(
    verifications: List[BetVerification],
    *,
    today: str = "",
) -> Dict[str, Any]:
    """Aggregate per-bet results into the summary the operator reads.

    Counts by result_code, plus per-result-code rows for inspection,
    plus the oldest stale_filled bet's age in days (for the daily-
    review block's threshold check)."""
    counts = Counter(v.result_code for v in verifications)
    n_total = len(verifications)
    n_filled_or_settled = sum(
        1 for v in verifications if v.result_code != "not_filled"
    )
    # Buckets per result_code so the operator can drill into each row.
    by_code: Dict[str, List[Dict[str, Any]]] = {}
    for v in verifications:
        by_code.setdefault(v.result_code, []).append(v.to_dict())
    # Oldest stale_filled bet's age (used by daily-review block to
    # decide whether to escalate from "note" to "alert").
    today = today or datetime.now(timezone.utc).date().isoformat()
    stale_filled = [
        v for v in verifications if v.result_code == "stale_filled"
    ]
    oldest_stale_days: Optional[int] = None
    for v in stale_filled:
        d = days_between(today, v.session_date)
        if d is not None and (
            oldest_stale_days is None or d > oldest_stale_days
        ):
            oldest_stale_days = d

    missing_share = (
        counts.get("missing_mlb_data", 0) / n_filled_or_settled
        if n_filled_or_settled else 0.0
    )

    return {
        "generated_at_utc": _now_iso(),
        "schema_version": 1,
        "today": today,
        "counts": {
            "total_bets_seen": n_total,
            "filled_or_settled_total": n_filled_or_settled,
            **dict(counts.most_common()),
        },
        "ok_share": (
            round(counts.get("ok", 0) / n_filled_or_settled, 4)
            if n_filled_or_settled else None
        ),
        "missing_mlb_data_share": round(missing_share, 4),
        "oldest_stale_filled_age_days": oldest_stale_days,
        "by_result_code": by_code,
        "thresholds": {
            "stale_filled_alert": STALE_FILLED_ALERT_THRESHOLD,
            "missing_mlb_data_rate_alert": MISSING_MLB_DATA_RATE_ALERT_THRESHOLD,
            "stale_oldest_days_alert": STALE_OLDEST_DAYS_ALERT_THRESHOLD,
        },
    }


def render_markdown(payload: Dict[str, Any]) -> str:
    lines: List[str] = []
    lines.append("# Settlement Truth Verification")
    lines.append("")
    lines.append(f"Generated: {payload['generated_at_utc']}")
    lines.append("")
    counts = payload["counts"]
    lines.append("## Counts by result code")
    for code, n in counts.items():
        lines.append(f"- `{code}`: {n}")
    lines.append("")
    lines.append("## Headline metrics")
    lines.append(f"- ok share: {payload.get('ok_share')}")
    lines.append(
        f"- missing_mlb_data share: {payload.get('missing_mlb_data_share')}"
    )
    lines.append(
        f"- oldest stale_filled age (days): "
        f"{payload.get('oldest_stale_filled_age_days')}"
    )
    lines.append("")
    # Surface the most critical rows first
    for code in (
        "resolution_mismatch", "total_mismatch", "stale_filled",
        "game_not_final_yet", "missing_mlb_data",
    ):
        rows = payload.get("by_result_code", {}).get(code) or []
        if not rows:
            continue
        lines.append(f"## `{code}` ({len(rows)} bets)")
        for r in rows[:25]:  # cap markdown verbosity
            lines.append(
                f"- {r.get('session_date')} game={r.get('game_pk')} "
                f"line={r.get('line')} side={r.get('side')} -- {r.get('notes')}"
            )
        if len(rows) > 25:
            lines.append(f"- (+{len(rows) - 25} more in JSON)")
        lines.append("")
    return "\n".join(lines) + "\n"


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Cross-check settled bets against MLB ground truth."
    )
    p.add_argument(
        "--mode", choices=["live", "paper", "both"], default="live",
        help=(
            "Which session source to verify. Live is the default; "
            "paper bets do not need settlement-truth verification "
            "(paper P&L is synthetic) but are supported for diagnostics."
        ),
    )
    p.add_argument("--sessions-dir", type=Path, default=None)
    p.add_argument("--paper-sessions-dir", type=Path, default=None)
    p.add_argument("--games-root", type=Path, default=DEFAULT_GAMES_ROOT)
    p.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    p.add_argument("--min-date", type=str, default="")
    p.add_argument("--max-date", type=str, default="")
    p.add_argument(
        "--today", type=str, default="",
        help=(
            "Reference date for stale-fill age calculation. Defaults "
            "to today's UTC date."
        ),
    )
    return p.parse_args(argv)


def run_verification(
    *,
    sessions_dir: Path,
    games_root: Path,
    min_date: str = "",
    max_date: str = "",
) -> List[BetVerification]:
    """Walk every session in `sessions_dir`, verify each bet against
    the local MLB JSON, and return the flat verification list."""
    out: List[BetVerification] = []
    for path in iterate_sessions(
        sessions_dir, min_date=min_date, max_date=max_date,
    ):
        session_date = _session_date_from_path(path)
        bets = load_session_bets(path)
        # Cache game state per game_pk to avoid duplicate disk reads
        # for sessions with multiple bets on the same game.
        seen: Dict[int, GameFinalState] = {}
        for bet in bets:
            game_pk = int(bet.get("game_pk") or 0)
            if game_pk not in seen:
                seen[game_pk] = load_game_final_state(
                    games_root, game_pk, session_date,
                )
            out.append(
                verify_bet(bet, seen[game_pk], session_date=session_date)
            )
    return out


def main(argv: Optional[List[str]] = None) -> None:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s %(message)s",
    )
    sessions_dir = args.sessions_dir or (
        DEFAULT_SESSIONS_DIR if args.mode != "paper" else DEFAULT_PAPER_SESSIONS_DIR
    )
    paper_sessions_dir = args.paper_sessions_dir or DEFAULT_PAPER_SESSIONS_DIR

    verifications: List[BetVerification] = []
    if args.mode in {"live", "both"}:
        verifications.extend(run_verification(
            sessions_dir=sessions_dir,
            games_root=args.games_root,
            min_date=args.min_date, max_date=args.max_date,
        ))
    if args.mode in {"paper", "both"}:
        verifications.extend(run_verification(
            sessions_dir=paper_sessions_dir,
            games_root=args.games_root,
            min_date=args.min_date, max_date=args.max_date,
        ))

    payload = build_report(verifications, today=args.today)
    payload["config"] = {
        "mode": args.mode,
        "sessions_dir": str(sessions_dir),
        "paper_sessions_dir": str(paper_sessions_dir),
        "games_root": str(args.games_root),
        "min_date": args.min_date or None,
        "max_date": args.max_date or None,
    }

    args.output_root.mkdir(parents=True, exist_ok=True)
    json_path = args.output_root / "settlement_truth_report.json"
    md_path = args.output_root / "settlement_truth_report.md"
    json_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    md_path.write_text(render_markdown(payload), encoding="utf-8")
    LOGGER.info(
        "Wrote %s (counts=%s, oldest_stale=%s)",
        json_path,
        payload["counts"],
        payload.get("oldest_stale_filled_age_days"),
    )


if __name__ == "__main__":
    main()
