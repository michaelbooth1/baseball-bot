#!/usr/bin/env python3
"""build_feed_enrichment.py -- Tier-3 offline feed enrichment (2026-05-29).

Reconstructs decision-time pitcher / lineup / bullpen context for each
model-bearing candidate by joining it to the MLB live-feed JSON we ALREADY
scrape for completed games (`data/games/regular/.../<game_pk>.json` carries
`liveData.plays.allPlays` with per-pitch events + timestamps). NO live
polling, no monitor changes, zero added fetch cost -- and it backfills the
full history.

For each candidate (joined by `candidate_id`) it finds the play active at
the candidate's `ts` and computes, as-of-ts:
  - pitch_count            current pitcher's cumulative pitches
  - batters_faced          current pitcher's PAs faced
  - times_through_order    (batters_faced - 1)//9 + 1   (the TTO penalty)
  - pitcher_is_starter     was this pitcher the game's starter for its side
  - defending_pitchers_used / relievers_used   bullpen depth proxy
  - p_throws / b_bats / platoon_batter_advantage   handedness matchup
  - catcher_id / catcher_name   defending battery (framing proxy, best-effort)
  - last_pitch_velo / recent_avg_velo / stint_velo_trend   fatigue proxy
  - last_exit_velo / recent_avg_exit_velo / recent_hardhit_count  contact quality
plus join-audit fields (feed_found, feed_match_quality, feed_state_agrees,
feed_pitcher_matches_candidate) so weak matches are filterable downstream.

The matched unit is the model-bearing candidate (reached the FV phase). The
match is primarily by `ts` (both UTC) with inning/half as a sanity cross-
check; the feed is authoritative for the pitcher.

Output:
  data/analysis_output/feed_enrichment/feed_enrichment.jsonl
  data/analysis_output/feed_enrichment/feed_enrichment.csv
  data/analysis_output/feed_enrichment/feed_enrichment_summary.json
"""
from __future__ import annotations

import argparse
import csv
import glob
import json
import math
import os
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean
from typing import Any, Dict, List, Optional, Sequence, Tuple

PROJECT_DIR = Path(__file__).resolve().parents[2]
DEFAULT_ROOTS = [
    PROJECT_DIR / "data" / "live_trading" / "candidate_universe",
    PROJECT_DIR / "data" / "paper_trading" / "candidate_universe",
    PROJECT_DIR / "data" / "paper_A_current" / "candidate_universe",
]
DEFAULT_GAMES_ROOT = PROJECT_DIR / "data" / "games" / "regular"
DEFAULT_OUTPUT_DIR = PROJECT_DIR / "data" / "analysis_output" / "feed_enrichment"
DEFAULT_OUTPUT_STEM = "feed_enrichment"

RECENT_PITCH_WINDOW = 5
RECENT_CONTACT_WINDOW = 3
HARD_HIT_MPH = 95.0

OUTPUT_COLUMNS = [
    "candidate_id", "game_pk", "line", "ts", "session_date", "mode",
    "decision", "decision_reason", "signal_model_family",
    "feed_found", "feed_match_quality", "feed_state_agrees",
    "feed_pitcher_matches_candidate", "matched_play_inning",
    "matched_play_half", "matched_atbat_index",
    "feed_pitcher_id", "feed_pitcher_name",
    "pitch_count", "batters_faced", "times_through_order", "pitcher_is_starter",
    "defending_pitchers_used", "relievers_used",
    "p_throws", "b_bats", "platoon_batter_advantage",
    "catcher_id", "catcher_name",
    "n_pitches_seen", "last_pitch_velo", "recent_avg_velo", "stint_velo_trend",
    "last_exit_velo", "recent_avg_exit_velo", "recent_hardhit_count",
]


# --------------------------------------------------------------------------
# Pure helpers
# --------------------------------------------------------------------------
def parse_iso(value: Any) -> Optional[datetime]:
    if not value:
        return None
    s = str(value).strip()
    if not s:
        return None
    s = s.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def half_from_inning_state(inning_state: Any) -> str:
    s = str(inning_state or "").strip().lower()
    if s.startswith("t"):
        return "top"
    if s.startswith("b"):
        return "bottom"
    return ""


def defending_side(half: str) -> str:
    """Side that is pitching/defending. Top half -> home pitches; bottom -> away."""
    if half == "top":
        return "home"
    if half == "bottom":
        return "away"
    return ""


def platoon_batter_advantage(p_throws: str, b_bats: str) -> Optional[bool]:
    p = (p_throws or "").strip().upper()
    b = (b_bats or "").strip().upper()
    if not p or not b:
        return None
    if b == "S":  # switch hitter always takes the platoon side
        return True
    return p != b  # opposite hands -> batter advantage


def _safe_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(f) or math.isinf(f):
        return None
    return f


def _safe_int(value: Any) -> Optional[int]:
    f = _safe_float(value)
    return int(f) if f is not None else None


# --------------------------------------------------------------------------
# Per-game timeline (built once per feed, reused across that game's candidates)
# --------------------------------------------------------------------------
@dataclass
class Pitch:
    t: datetime
    pitcher_id: Optional[int]
    side: str           # defending side that threw it
    velo: Optional[float]


@dataclass
class PA:
    start: Optional[datetime]
    end: Optional[datetime]
    atbat: Optional[int]
    pitcher_id: Optional[int]
    pitcher_name: str
    side: str           # defending (pitching) side
    inning: Optional[int]
    half: str
    p_throws: str
    b_bats: str


@dataclass
class InPlay:
    t: datetime
    batting_side: str
    exit_velo: Optional[float]


@dataclass
class GameTimeline:
    pas: List[PA] = field(default_factory=list)
    pitches: List[Pitch] = field(default_factory=list)
    in_play: List[InPlay] = field(default_factory=list)
    starter_by_side: Dict[str, Optional[int]] = field(default_factory=dict)
    catcher_by_side: Dict[str, Tuple[Optional[int], str]] = field(default_factory=dict)


def _catcher_for_side(boxscore_team: Dict[str, Any]) -> Tuple[Optional[int], str]:
    """Best-effort primary catcher for a team from the boxscore players map."""
    players = (boxscore_team or {}).get("players", {}) or {}
    best: Tuple[Optional[int], str] = (None, "")
    best_order = None
    for pdata in players.values():
        pos = (pdata.get("position", {}) or {}).get("abbreviation")
        if pos != "C":
            continue
        person = pdata.get("person", {}) or {}
        pid = _safe_int(person.get("id"))
        name = str(person.get("fullName") or "")
        order = pdata.get("battingOrder")  # starters have one
        try:
            order_i = int(order) if order is not None else None
        except (TypeError, ValueError):
            order_i = None
        # Prefer a catcher with a batting-order slot (i.e., started); else first seen.
        if best[0] is None or (order_i is not None and best_order is None):
            best = (pid, name)
            best_order = order_i
    return best


def build_timeline(feed: Dict[str, Any]) -> GameTimeline:
    live = (feed or {}).get("liveData", {}) or {}
    plays = (live.get("plays", {}) or {}).get("allPlays", []) or []
    tl = GameTimeline()
    first_pitcher_by_side: Dict[str, Optional[int]] = {}
    for play in plays:
        if not isinstance(play, dict):
            continue
        about = play.get("about", {}) or {}
        matchup = play.get("matchup", {}) or {}
        half = str(about.get("halfInning") or "").strip().lower()
        side = defending_side("top" if half.startswith("t") else "bottom" if half.startswith("b") else "")
        batting_side = "away" if side == "home" else "home" if side == "away" else ""
        pitcher = matchup.get("pitcher", {}) or {}
        pid = _safe_int(pitcher.get("id"))
        pname = str(pitcher.get("fullName") or "")
        start = parse_iso(about.get("startTime"))
        end = parse_iso(play.get("playEndTime") or about.get("endTime"))
        inning = _safe_int(about.get("inning"))
        p_throws = str((matchup.get("pitchHand", {}) or {}).get("code") or "")
        b_bats = str((matchup.get("batSide", {}) or {}).get("code") or "")
        tl.pas.append(PA(
            start=start, end=end, atbat=_safe_int(play.get("atBatIndex")),
            pitcher_id=pid, pitcher_name=pname, side=side, inning=inning,
            half="top" if half.startswith("t") else "bottom" if half.startswith("b") else "",
            p_throws=p_throws, b_bats=b_bats,
        ))
        if side and side not in first_pitcher_by_side and pid is not None:
            first_pitcher_by_side[side] = pid
        for ev in play.get("playEvents", []) or []:
            if not isinstance(ev, dict):
                continue
            ev_t = parse_iso(ev.get("startTime"))
            if ev.get("isPitch"):
                velo = _safe_float((ev.get("pitchData", {}) or {}).get("startSpeed"))
                if ev_t is not None:
                    tl.pitches.append(Pitch(t=ev_t, pitcher_id=pid, side=side, velo=velo))
            hit = ev.get("hitData", {}) or {}
            if hit and ev_t is not None and batting_side:
                tl.in_play.append(InPlay(
                    t=ev_t, batting_side=batting_side,
                    exit_velo=_safe_float(hit.get("launchSpeed")),
                ))
    tl.starter_by_side = first_pitcher_by_side
    teams = (live.get("boxscore", {}) or {}).get("teams", {}) or {}
    for side in ("home", "away"):
        tl.catcher_by_side[side] = _catcher_for_side(teams.get(side, {}) or {})
    # Keep timelines sorted by time for as-of filtering.
    tl.pitches.sort(key=lambda p: p.t)
    tl.in_play.sort(key=lambda p: p.t)
    return tl


def find_pa_at(tl: GameTimeline, ts: datetime) -> Tuple[Optional[PA], str]:
    """Return (matched_PA, quality) for a candidate timestamp."""
    candidates = [pa for pa in tl.pas if pa.start is not None]
    if not candidates:
        return None, "no_plays"
    before = [pa for pa in candidates if pa.start <= ts]
    if not before:
        return None, "pregame"
    matched = max(before, key=lambda pa: pa.start)
    if matched.end is not None and matched.end >= ts:
        return matched, "in_play_window"
    return matched, "last_before"


# --------------------------------------------------------------------------
# Enrichment
# --------------------------------------------------------------------------
def enrich_candidate(tl: GameTimeline, cand: Dict[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {
        "feed_found": True,
        "feed_match_quality": None,
        "feed_state_agrees": None,
        "feed_pitcher_matches_candidate": None,
        "matched_play_inning": None,
        "matched_play_half": None,
        "matched_atbat_index": None,
        "feed_pitcher_id": None, "feed_pitcher_name": None,
        "pitch_count": None, "batters_faced": None, "times_through_order": None,
        "pitcher_is_starter": None,
        "defending_pitchers_used": None, "relievers_used": None,
        "p_throws": None, "b_bats": None, "platoon_batter_advantage": None,
        "catcher_id": None, "catcher_name": None,
        "n_pitches_seen": None, "last_pitch_velo": None, "recent_avg_velo": None,
        "stint_velo_trend": None,
        "last_exit_velo": None, "recent_avg_exit_velo": None, "recent_hardhit_count": None,
    }
    ts = parse_iso(cand.get("ts"))
    if ts is None:
        out["feed_match_quality"] = "no_ts"
        return out
    pa, quality = find_pa_at(tl, ts)
    out["feed_match_quality"] = quality
    if pa is None:
        return out

    pid = pa.pitcher_id
    side = pa.side
    out["matched_play_inning"] = pa.inning
    out["matched_play_half"] = pa.half
    out["matched_atbat_index"] = pa.atbat
    out["feed_pitcher_id"] = pid
    out["feed_pitcher_name"] = pa.pitcher_name or None
    out["p_throws"] = pa.p_throws or None
    out["b_bats"] = pa.b_bats or None
    out["platoon_batter_advantage"] = platoon_batter_advantage(pa.p_throws, pa.b_bats)

    # state cross-check vs the (schedule-stale) candidate fields
    cand_half = half_from_inning_state(cand.get("inning_state"))
    cand_inning = _safe_int(cand.get("inning"))
    out["feed_state_agrees"] = bool(
        cand_inning is not None and pa.inning == cand_inning
        and (not cand_half or cand_half == pa.half)
    )
    cand_pid = _safe_int(cand.get("current_pitcher_id"))
    if cand_pid is not None and pid is not None:
        out["feed_pitcher_matches_candidate"] = bool(cand_pid == pid)

    # Pitcher load as-of-ts (PAs started at/before ts and pitched by this pitcher).
    pitcher_pas = [pa2 for pa2 in tl.pas
                   if pa2.pitcher_id == pid and pa2.start is not None and pa2.start <= ts]
    bf = len(pitcher_pas)
    out["batters_faced"] = bf
    out["times_through_order"] = ((bf - 1) // 9 + 1) if bf > 0 else 0
    if side:
        out["pitcher_is_starter"] = bool(tl.starter_by_side.get(side) == pid and pid is not None)
        used = {pa2.pitcher_id for pa2 in tl.pas
                if pa2.side == side and pa2.pitcher_id is not None
                and pa2.start is not None and pa2.start <= ts}
        out["defending_pitchers_used"] = len(used)
        out["relievers_used"] = max(0, len(used) - 1)
        cat_id, cat_name = tl.catcher_by_side.get(side, (None, ""))
        out["catcher_id"] = cat_id
        out["catcher_name"] = cat_name or None

    pitches = [p for p in tl.pitches if p.pitcher_id == pid and p.t <= ts]
    out["pitch_count"] = len(pitches)
    velos = [p.velo for p in pitches if p.velo is not None]
    out["n_pitches_seen"] = len(velos)
    if velos:
        out["last_pitch_velo"] = round(velos[-1], 2)
        out["recent_avg_velo"] = round(mean(velos[-RECENT_PITCH_WINDOW:]), 2)
        if len(velos) >= 2 * RECENT_PITCH_WINDOW:
            first = mean(velos[:RECENT_PITCH_WINDOW])
            out["stint_velo_trend"] = round(mean(velos[-RECENT_PITCH_WINDOW:]) - first, 2)

    batting_side = "away" if side == "home" else "home" if side == "away" else ""
    contacts = [ip for ip in tl.in_play
                if ip.batting_side == batting_side and ip.exit_velo is not None and ip.t <= ts]
    if contacts:
        out["last_exit_velo"] = round(contacts[-1].exit_velo, 2)
        recent = contacts[-RECENT_CONTACT_WINDOW:]
        out["recent_avg_exit_velo"] = round(mean(c.exit_velo for c in recent), 2)
        out["recent_hardhit_count"] = sum(1 for c in recent if c.exit_velo >= HARD_HIT_MPH)
    return out


# --------------------------------------------------------------------------
# Loading
# --------------------------------------------------------------------------
def build_game_pk_index(games_root: Path) -> Dict[int, str]:
    """Map game_pk -> feed path from filenames (no file opens)."""
    index: Dict[int, str] = {}
    for p in glob.glob(os.path.join(str(games_root), "**", "*.json"), recursive=True):
        stem = os.path.splitext(os.path.basename(p))[0]
        try:
            index[int(stem)] = p
        except ValueError:
            continue
    return index


def _is_model_bearing(row: Dict[str, Any]) -> bool:
    """Reached the FV phase (base/raw FV computed) or placed/UNDER-emitted."""
    if row.get("base_fair_value") is not None or row.get("fair_value_raw") is not None:
        return True
    decision = str(row.get("decision") or "")
    return decision in {"trade", "paper_under", "live_under", "shadow_under"}


def collect_candidates(
    roots: Sequence[Path], *, model_bearing_only: bool = True,
) -> List[Dict[str, Any]]:
    seen: set = set()
    out: List[Dict[str, Any]] = []
    for root in roots:
        for p in glob.glob(os.path.join(str(root), "*_candidates.jsonl")):
            for line in open(p, encoding="utf-8"):
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                cid = d.get("candidate_id")
                gpk = _safe_int(d.get("game_pk"))
                if not cid or gpk is None or not d.get("ts"):
                    continue
                if cid in seen:
                    continue
                if model_bearing_only and not _is_model_bearing(d):
                    continue
                seen.add(cid)
                out.append(d)
    return out


# --------------------------------------------------------------------------
# Report assembly
# --------------------------------------------------------------------------
def build_rows(
    candidates: Sequence[Dict[str, Any]],
    game_index: Dict[int, str],
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    by_game: Dict[int, List[Dict[str, Any]]] = defaultdict(list)
    for c in candidates:
        by_game[int(c["game_pk"])].append(c)

    rows: List[Dict[str, Any]] = []
    games_with_feed = 0
    games_without_feed = 0
    for gpk, cands in by_game.items():
        path = game_index.get(gpk)
        if not path:
            games_without_feed += 1
            for c in cands:
                rows.append({**_identity(c), "feed_found": False})
            continue
        try:
            with open(path, encoding="utf-8") as f:
                feed = json.load(f)
            timeline = build_timeline(feed)
        except Exception:
            games_without_feed += 1
            for c in cands:
                rows.append({**_identity(c), "feed_found": False})
            continue
        games_with_feed += 1
        for c in cands:
            rows.append({**_identity(c), **enrich_candidate(timeline, c)})

    quality_counts = Counter(r.get("feed_match_quality") for r in rows)
    matched = [r for r in rows if r.get("pitch_count") is not None]
    summary = {
        "n_candidates": len(rows),
        "n_games": len(by_game),
        "games_with_feed": games_with_feed,
        "games_without_feed": games_without_feed,
        "n_enriched": len(matched),
        "match_quality": dict(quality_counts),
        "feed_state_agree_rate": (
            round(sum(1 for r in rows if r.get("feed_state_agrees")) / len(rows), 4)
            if rows else None
        ),
        "pitcher_match_rate": (
            round(
                sum(1 for r in rows if r.get("feed_pitcher_matches_candidate"))
                / max(1, sum(1 for r in rows if r.get("feed_pitcher_matches_candidate") is not None)),
                4,
            )
            if rows else None
        ),
        "avg_pitch_count": (round(mean(r["pitch_count"] for r in matched), 1) if matched else None),
        "tto_distribution": dict(Counter(r.get("times_through_order") for r in matched)),
    }
    return rows, summary


def _identity(c: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "candidate_id": c.get("candidate_id"),
        "game_pk": _safe_int(c.get("game_pk")),
        "line": c.get("line"),
        "ts": c.get("ts"),
        "session_date": c.get("session_date"),
        "mode": c.get("mode"),
        "decision": c.get("decision"),
        "decision_reason": c.get("decision_reason"),
        "signal_model_family": c.get("signal_model_family"),
    }


def write_outputs(rows: Sequence[Dict[str, Any]], summary: Dict[str, Any],
                  output_root: Path, stem: str) -> None:
    output_root.mkdir(parents=True, exist_ok=True)
    with open(output_root / f"{stem}.jsonl", "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    with open(output_root / f"{stem}.csv", "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=OUTPUT_COLUMNS, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)
    manifest = {
        "schema_version": 1,
        "generated_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "summary": summary,
    }
    with open(output_root / f"{stem}_summary.json", "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--roots", type=str, default=None,
                   help="Comma-separated candidate_universe dirs (default: live + paper + paper_A_current).")
    p.add_argument("--games-root", type=Path, default=DEFAULT_GAMES_ROOT)
    p.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_DIR)
    p.add_argument("--output-stem", type=str, default=DEFAULT_OUTPUT_STEM)
    p.add_argument("--all-rows", action="store_true",
                   help="Enrich ALL candidate rows, not just model-bearing ones.")
    return p.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    roots = (
        [Path(s.strip()) for s in args.roots.split(",") if s.strip()]
        if args.roots else list(DEFAULT_ROOTS)
    )
    candidates = collect_candidates(roots, model_bearing_only=not args.all_rows)
    game_index = build_game_pk_index(args.games_root)
    rows, summary = build_rows(candidates, game_index)
    write_outputs(rows, summary, args.output_root, args.output_stem)
    print(f"[feed_enrichment] candidates={summary['n_candidates']} "
          f"games={summary['n_games']} with_feed={summary['games_with_feed']} "
          f"enriched={summary['n_enriched']} avg_pitch_count={summary['avg_pitch_count']}")
    print(f"  match_quality={summary['match_quality']}")
    print(f"  wrote {args.output_root / (args.output_stem + '.jsonl')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
