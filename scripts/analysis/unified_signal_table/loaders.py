"""Input loaders for sessions, ledgers, candidate rows, and book captures."""

from __future__ import annotations

import json
import re
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from scripts.analysis.unified_signal_table.schema import CaptureData
from scripts.analysis.unified_signal_table.utils import _date_in_range, _read_json, _read_jsonl

SESSION_FILE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})_session\.json$")
CANDIDATE_FILE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})_candidates\.jsonl$")

def _iter_session_files(
    sessions_root: Path,
    min_date: Optional[str],
    max_date: Optional[str],
) -> List[Tuple[str, Path]]:
    if not sessions_root.exists():
        return []
    out: List[Tuple[str, Path]] = []
    for path in sorted(sessions_root.glob("*_session.json")):
        m = SESSION_FILE_RE.match(path.name)
        if not m:
            continue
        session_date = m.group(1)
        if not _date_in_range(session_date, min_date, max_date):
            continue
        out.append((session_date, path))
    return out


def load_sessions_for_mode(
    mode: str,
    sessions_root: Path,
    min_date: Optional[str],
    max_date: Optional[str],
    warnings: List[str],
    hard_errors: List[str],
) -> Dict[str, Dict[str, Any]]:
    session_rows: Dict[str, Dict[str, Any]] = {}
    for session_date, path in _iter_session_files(sessions_root, min_date, max_date):
        try:
            payload = _read_json(path)
        except Exception as exc:
            hard_errors.append(f"[{mode}] failed to read session file {path}: {exc}")
            continue

        params = payload.get("params", {}) or {}
        bets = payload.get("bets", []) or []
        if not isinstance(bets, list):
            hard_errors.append(f"[{mode}] session file has non-list bets: {path}")
            continue

        for bet in bets:
            if not isinstance(bet, dict):
                warnings.append(f"[{mode}] non-dict bet in {path}")
                continue
            bet_id = str(bet.get("bet_id") or "")
            if not bet_id:
                warnings.append(f"[{mode}] bet without bet_id in {path}")
                continue
            if bet_id in session_rows:
                hard_errors.append(
                    f"[{mode}] duplicate bet_id in session files: {bet_id} "
                    f"({session_rows[bet_id]['session_path']} and {path})"
                )
                continue
            session_rows[bet_id] = {
                "mode": mode,
                "session_date": session_date,
                "session_path": str(path),
                "session_params": params,
                "bet": bet,
            }
    return session_rows


def load_ledger_events_for_mode(
    mode: str,
    ledger_path: Path,
    warnings: List[str],
) -> Dict[str, List[Dict[str, Any]]]:
    grouped: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    if not ledger_path.exists():
        warnings.append(f"[{mode}] ledger path does not exist: {ledger_path}")
        return grouped

    with open(ledger_path, encoding="utf-8") as f:
        for i, raw in enumerate(f, start=1):
            raw = raw.strip()
            if not raw:
                continue
            try:
                event = json.loads(raw)
            except Exception as exc:
                warnings.append(f"[{mode}] malformed ledger JSON line {i}: {exc}")
                continue
            if not isinstance(event, dict):
                warnings.append(f"[{mode}] non-dict ledger event at line {i}")
                continue
            bet_id = str(event.get("bet_id") or "")
            if not bet_id:
                warnings.append(f"[{mode}] ledger event line {i} missing bet_id")
                continue
            grouped[bet_id].append(event)
    return grouped


def load_candidate_trade_rows_for_mode(
    mode: str,
    candidates_root: Path,
    min_date: Optional[str],
    max_date: Optional[str],
    warnings: List[str],
    hard_errors: List[str],
) -> Dict[str, Dict[str, Any]]:
    """Load trade candidate rows keyed by bet_id.

    Candidate rows carry decision-time state-value diagnostics that are richer
    than the compact bet/session record. Only rows with an actual bet_id are
    merged into signals_master; shadow-only candidates remain in the candidate
    universe analysis path.
    """

    out: Dict[str, Dict[str, Any]] = {}
    if not candidates_root.exists():
        return out

    for path in sorted(candidates_root.glob("*_candidates.jsonl")):
        m = CANDIDATE_FILE_RE.match(path.name)
        if not m:
            continue
        session_date = m.group(1)
        if not _date_in_range(session_date, min_date, max_date):
            continue
        try:
            with open(path, "r", encoding="utf-8") as f:
                for i, raw in enumerate(f, start=1):
                    raw = raw.strip()
                    if not raw:
                        continue
                    try:
                        row = json.loads(raw)
                    except json.JSONDecodeError as exc:
                        warnings.append(f"[{mode}] bad candidate JSON {path}:{i}: {exc}")
                        continue
                    if not isinstance(row, dict):
                        warnings.append(f"[{mode}] non-dict candidate row {path}:{i}")
                        continue
                    if str(row.get("decision") or "") != "trade":
                        continue
                    bet_id = str(row.get("bet_id") or "")
                    if not bet_id:
                        continue
                    if bet_id in out:
                        warnings.append(f"[{mode}] duplicate trade candidate bet_id={bet_id} in {path}:{i}")
                        continue
                    out[bet_id] = row
        except Exception as exc:
            hard_errors.append(f"[{mode}] failed to read candidate file {path}: {exc}")
    return out


def _iter_capture_files(
    captures_root: Path,
    min_date: Optional[str],
    max_date: Optional[str],
) -> List[Path]:
    out: List[Path] = []
    if not captures_root.exists():
        return out
    for day_dir in sorted(captures_root.iterdir()):
        if not day_dir.is_dir():
            continue
        date_str = day_dir.name
        if not _date_in_range(date_str, min_date, max_date):
            continue
        out.extend(sorted(day_dir.glob("*.jsonl")))
    return out


def load_captures_for_mode(
    mode: str,
    captures_root: Path,
    min_date: Optional[str],
    max_date: Optional[str],
    warnings: List[str],
    hard_errors: List[str],
) -> Dict[str, CaptureData]:
    captures: Dict[str, CaptureData] = {}
    files = _iter_capture_files(captures_root, min_date, max_date)
    for path in files:
        try:
            rows = _read_jsonl(path)
        except Exception as exc:
            warnings.append(f"[{mode}] failed to read capture file {path}: {exc}")
            continue
        if not rows:
            warnings.append(f"[{mode}] empty capture file: {path}")
            continue

        header = rows[0] if isinstance(rows[0], dict) else {}
        if header.get("type") != "signal":
            warnings.append(f"[{mode}] capture file missing signal header: {path}")
            continue

        bet_id = str(header.get("bet_id") or "")
        if not bet_id:
            warnings.append(f"[{mode}] capture header missing bet_id: {path}")
            continue

        snapshots = [r for r in rows[1:] if isinstance(r, dict) and r.get("type") == "snapshot"]
        seqs = [s.get("seq") for s in snapshots if isinstance(s.get("seq"), int)]
        if len(seqs) != len(snapshots):
            warnings.append(f"[{mode}] non-integer snapshot seq values in {path}")
        if seqs and seqs != sorted(seqs):
            hard_errors.append(f"[{mode}] non-monotonic snapshot seq in {path}")

        t0 = {}
        for s in snapshots:
            if s.get("seq") == 0:
                t0 = s.get("book", {}) or {}
                break
        if not t0 and snapshots:
            t0 = snapshots[0].get("book", {}) or {}

        if bet_id in captures:
            warnings.append(
                f"[{mode}] duplicate capture for bet_id={bet_id}; keeping first "
                f"({captures[bet_id].path}), ignoring {path}"
            )
            continue

        captures[bet_id] = CaptureData(
            bet_id=bet_id,
            path=str(path),
            header=header,
            t0_book=t0,
            snapshots=snapshots,
        )
    return captures

