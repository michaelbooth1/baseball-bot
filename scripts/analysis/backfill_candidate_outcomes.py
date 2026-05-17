#!/usr/bin/env python3
"""
Backfill candidate outcome rows from local game files.

Why this exists:
- During live runs, outcomes JSONL rows are written only while the process is
  active and a game is observed as final.
- If the process stops early, candidate rows can exist without matching
  outcomes rows, which blocks shadow-gate evaluation.

This script scans candidate_universe files and appends missing outcome rows for
each unique (session_date, mode, game_pk, line) key when final scores are
available in local data/games/regular game files.
"""

from __future__ import annotations

import argparse
import json
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple


PROJECT_DIR = Path(__file__).resolve().parents[2]
DEFAULT_LIVE_CANDIDATE_ROOT = PROJECT_DIR / "data" / "live_trading" / "candidate_universe"
DEFAULT_PAPER_CANDIDATE_ROOT = PROJECT_DIR / "data" / "paper_trading" / "candidate_universe"
DEFAULT_GAMES_ROOT = PROJECT_DIR / "data" / "games" / "regular"

FILE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})_candidates\.jsonl$")


@dataclass(frozen=True)
class OutcomeKey:
    session_date: str
    mode: str
    game_pk: int
    line: str


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _safe_int(v: object) -> Optional[int]:
    try:
        if v is None or v == "":
            return None
        return int(v)
    except Exception:
        return None


def _normalize_line(v: object) -> Optional[str]:
    if v is None:
        return None
    s = str(v).strip()
    if not s:
        return None
    try:
        return f"{float(s):.1f}"
    except Exception:
        return s


def _date_in_range(session_date: str, min_date: Optional[str], max_date: Optional[str]) -> bool:
    if min_date and session_date < min_date:
        return False
    if max_date and session_date > max_date:
        return False
    return True


def _iter_candidate_files(root: Path) -> Iterable[Tuple[str, Path]]:
    if not root.exists():
        return
    for path in sorted(root.glob("*_candidates.jsonl")):
        m = FILE_RE.match(path.name)
        if not m:
            continue
        yield m.group(1), path


def _candidate_roots_for_mode(
    mode: str,
    live_root: Path,
    paper_root: Path,
) -> List[Tuple[str, Path]]:
    if mode == "live":
        return [("live", live_root)]
    if mode == "paper":
        return [("paper", paper_root)]
    return [("live", live_root), ("paper", paper_root)]


def _load_existing_outcome_keys(outcome_path: Path, session_date: str) -> set[OutcomeKey]:
    keys: set[OutcomeKey] = set()
    if not outcome_path.exists():
        return keys
    with open(outcome_path, encoding="utf-8-sig") as f:
        for raw in f:
            raw = raw.strip()
            if not raw:
                continue
            try:
                row = json.loads(raw)
            except Exception:
                continue
            game_pk = _safe_int(row.get("game_pk"))
            line = _normalize_line(row.get("line"))
            if game_pk is None or line is None:
                continue
            row_mode = str(row.get("mode") or "")
            row_date = str(row.get("session_date") or session_date)
            keys.add(OutcomeKey(row_date, row_mode, game_pk, line))
    return keys


def _candidate_game_paths(
    games_root: Path,
    session_date: str,
    game_pk: int,
    lookaround_days: int,
) -> List[Path]:
    try:
        base_day = date.fromisoformat(session_date)
    except Exception:
        return []

    offsets = [0]
    for d in range(1, max(0, lookaround_days) + 1):
        offsets.extend([-d, d])

    out: List[Path] = []
    for off in offsets:
        day = base_day + timedelta(days=off)
        p = games_root / f"{day.year:04d}" / f"{day.month:02d}" / f"{day.day:02d}" / f"{game_pk}.json"
        if p.exists():
            out.append(p)
    return out


def _load_final_score(game_path: Path) -> Optional[Tuple[int, int]]:
    try:
        with open(game_path, encoding="utf-8-sig") as f:
            d = json.load(f)
    except Exception:
        return None

    teams = (((d.get("liveData") or {}).get("linescore") or {}).get("teams") or {})
    away = _safe_int(((teams.get("away") or {}).get("runs")))
    home = _safe_int(((teams.get("home") or {}).get("runs")))
    if away is None or home is None:
        return None
    return away, home


def _find_game_file_by_pk(
    games_root: Path,
    game_pk: int,
    cache: Dict[int, Optional[Path]],
) -> Optional[Path]:
    cached = cache.get(game_pk)
    if cached is not None or game_pk in cache:
        return cached

    matches = sorted(games_root.glob(f"**/{game_pk}.json"))
    chosen = matches[-1] if matches else None
    cache[game_pk] = chosen
    return chosen


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Backfill missing candidate outcomes from local game files.")
    p.add_argument("--mode", choices=["live", "paper", "both"], default="both")
    p.add_argument("--min-date", type=str, default="", help="Inclusive lower date bound YYYY-MM-DD.")
    p.add_argument("--max-date", type=str, default="", help="Inclusive upper date bound YYYY-MM-DD.")
    p.add_argument("--live-root", type=Path, default=DEFAULT_LIVE_CANDIDATE_ROOT)
    p.add_argument("--paper-root", type=Path, default=DEFAULT_PAPER_CANDIDATE_ROOT)
    p.add_argument("--games-root", type=Path, default=DEFAULT_GAMES_ROOT)
    p.add_argument(
        "--lookaround-days",
        type=int,
        default=1,
        help="Check session_date plus/minus this many days for game files (default: 1).",
    )
    p.add_argument("--dry-run", action="store_true", help="Compute and print only; do not write files.")
    p.add_argument("--verbose", action="store_true")
    return p.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)

    min_date = args.min_date or None
    max_date = args.max_date or None
    now_iso = _now_iso()

    total_candidate_keys = 0
    total_existing_keys = 0
    total_missing_keys = 0
    total_resolved_scores = 0
    total_written_rows = 0
    total_unresolved = 0
    game_file_pk_cache: Dict[int, Optional[Path]] = {}

    for mode_label, root in _candidate_roots_for_mode(args.mode, args.live_root, args.paper_root):
        if not root.exists():
            print(f"[{mode_label}] candidate root does not exist: {root}")
            continue

        rows_to_append: Dict[Path, List[Dict[str, object]]] = defaultdict(list)
        mode_candidate_keys = 0
        mode_existing_keys = 0
        mode_missing_keys = 0
        mode_resolved = 0
        mode_unresolved = 0

        for session_date, candidate_path in _iter_candidate_files(root):
            if not _date_in_range(session_date, min_date=min_date, max_date=max_date):
                continue

            outcome_path = root / f"{session_date}_outcomes.jsonl"
            existing = _load_existing_outcome_keys(outcome_path, session_date)
            mode_existing_keys += len(existing)

            # Unique key -> representative metadata for output row.
            candidate_keys: Dict[OutcomeKey, Dict[str, object]] = {}
            with open(candidate_path, encoding="utf-8-sig") as f:
                for raw in f:
                    raw = raw.strip()
                    if not raw:
                        continue
                    try:
                        row = json.loads(raw)
                    except Exception:
                        continue
                    game_pk = _safe_int(row.get("game_pk"))
                    line = _normalize_line(row.get("line"))
                    if game_pk is None or line is None:
                        continue
                    row_mode = str(row.get("mode") or mode_label)
                    key = OutcomeKey(session_date=session_date, mode=row_mode, game_pk=game_pk, line=line)
                    if key not in candidate_keys:
                        candidate_keys[key] = {
                            "away_abbrev": str(row.get("away_abbrev") or ""),
                            "home_abbrev": str(row.get("home_abbrev") or ""),
                        }

            mode_candidate_keys += len(candidate_keys)

            for key, meta in candidate_keys.items():
                if key in existing:
                    continue
                mode_missing_keys += 1

                score: Optional[Tuple[int, int]] = None
                for game_path in _candidate_game_paths(
                    games_root=args.games_root,
                    session_date=key.session_date,
                    game_pk=key.game_pk,
                    lookaround_days=args.lookaround_days,
                ):
                    score = _load_final_score(game_path)
                    if score is not None:
                        break

                if score is None:
                    fallback_path = _find_game_file_by_pk(
                        games_root=args.games_root,
                        game_pk=key.game_pk,
                        cache=game_file_pk_cache,
                    )
                    if fallback_path is not None:
                        score = _load_final_score(fallback_path)

                if score is None:
                    mode_unresolved += 1
                    if args.verbose:
                        print(
                            f"[{mode_label}] unresolved final score for "
                            f"{key.session_date} game_pk={key.game_pk} line={key.line}"
                        )
                    continue

                final_away, final_home = score
                final_total = final_away + final_home
                line_val = float(key.line)
                rows_to_append[outcome_path].append(
                    {
                        "schema_version": 1,
                        "session_date": key.session_date,
                        "mode": key.mode,
                        "game_pk": key.game_pk,
                        "away_abbrev": meta["away_abbrev"],
                        "home_abbrev": meta["home_abbrev"],
                        "line": key.line,
                        "final_away": final_away,
                        "final_home": final_home,
                        "final_total": final_total,
                        "over_hit": bool(final_total > line_val),
                        "settled_at": now_iso,
                    }
                )
                mode_resolved += 1

        mode_written = 0
        for outcome_path, rows in sorted(rows_to_append.items(), key=lambda kv: str(kv[0])):
            rows_sorted = sorted(rows, key=lambda r: (int(r["game_pk"]), float(r["line"]), str(r["mode"])))
            mode_written += len(rows_sorted)
            if not args.dry_run:
                outcome_path.parent.mkdir(parents=True, exist_ok=True)
                with open(outcome_path, "a", encoding="utf-8") as f:
                    for row in rows_sorted:
                        f.write(json.dumps(row) + "\n")

        total_candidate_keys += mode_candidate_keys
        total_existing_keys += mode_existing_keys
        total_missing_keys += mode_missing_keys
        total_resolved_scores += mode_resolved
        total_written_rows += mode_written
        total_unresolved += mode_unresolved

        print(
            f"[{mode_label}] candidate_keys={mode_candidate_keys} existing_outcomes={mode_existing_keys} "
            f"missing={mode_missing_keys} resolved={mode_resolved} "
            f"{'would_write' if args.dry_run else 'written'}={mode_written} unresolved={mode_unresolved}"
        )

    print(
        "TOTAL "
        f"candidate_keys={total_candidate_keys} existing_outcomes={total_existing_keys} "
        f"missing={total_missing_keys} resolved={total_resolved_scores} "
        f"{'would_write' if args.dry_run else 'written'}={total_written_rows} unresolved={total_unresolved}"
    )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
