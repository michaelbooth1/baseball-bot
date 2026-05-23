#!/usr/bin/env python3
"""
Monitor Polymarket MLB O/U orderbooks for today's games.

What it does:
1) Pulls official MLB schedule for a date (default: today in selected timezone)
   from MLB StatsAPI and stores it locally.
2) Cross-references each scheduled game to Polymarket MLB event(s).
3) Discovers O/U markets (e.g. 8.5, 9.5) and token IDs.
4) Waits until games are live (or preview, if enabled), then polls CLOB books
   at a configurable tick interval.
5) Writes one JSONL file per game+line+side with best bid / best ask snapshots.

All outputs are written inside baseball/data/polymarket/mlb_ou by default.

Module layout
-------------
This file owns the orchestration class ``MLBPolymarketMonitor`` and the CLI
entry point. Everything else has been split out for readability:

- monitor_constants.py    URLs, defaults, regex, TEAM_SLUGS, LOGGER, debug cadences
- monitor_utils.py        _safe_float / _safe_int / slug + iso helpers
- monitor_system.py       performance-mode + sleep-prevention
- monitor_models.py       ScheduleScore / ScheduledGame / OUMarket / GameMarketMatch
- monitor_recorder.py     LocalRecorder (per-game JSONL writer)
- monitor_stats_client.py MLBStatsClient (schedule fetch + pitcher cache)
- monitor_discovery.py    PolymarketDiscoveryClient (Gamma event matching)
- monitor_book_client.py  PolymarketBookClient (CLOB + Gamma fallback)
- monitor_cli.py          parse_args

External callers (tests, scripts/trading) import directly from
``monitor_mlb_polymarket_ou``; the re-export block at the bottom of this file
keeps every legacy import path working.
"""

from __future__ import annotations

import argparse
import logging
import math
import threading  # noqa: F401  (re-exported for legacy callers)
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from zoneinfo import ZoneInfo

from monitor_book_client import PolymarketBookClient
from monitor_cli import parse_args
from monitor_constants import (
    CLOB_BASE,
    DEFAULT_BOOK_FAILURE_COOLDOWN_SECS,
    DEFAULT_BOOK_FAILURE_MAX_COOLDOWN_SECS,
    DEFAULT_BOOK_FAILURE_RETIRE_STREAK,
    DEFAULT_OUTPUT_ROOT,
    DEFAULT_PITCHER_CACHE_PATH,
    DEFAULT_TIMEZONE,
    DISCOVERY_NO_NEW_INFO_EVERY_N_CYCLES,
    FINAL_STATES,
    GAMMA_BASE,
    LIVE_STATES,
    LOGGER,
    MLB_AVG_ERA,
    MLB_SCHEDULE_URL,
    NOISY_LIBRARY_LOGGERS,
    OU_LINE_RE,
    PITCHER_CACHE_MAX_AGE_HOURS,
    PITCHER_CACHE_MIN_PITCHER_COUNT,
    PITCHER_CACHE_STALE_FALLBACK_MAX_AGE_HOURS,
    POLL_PROGRESS_DEBUG_EVERY_N_CYCLES,
    PREVIEW_STATES,
    SCHEDULE_CHANGED_DEBUG_EVERY_N_REFRESHES,
    SCHEDULE_UNCHANGED_DEBUG_EVERY_N_REFRESHES,
    TEAM_SLUGS,
    suppress_noisy_library_loggers,
)
from monitor_discovery import PolymarketDiscoveryClient
from monitor_models import GameMarketMatch, OUMarket, ScheduledGame, ScheduleScore
from monitor_recorder import LocalRecorder
from monitor_stats_client import MLBStatsClient
from monitor_system import _prevent_sleep, _setup_performance_mode
from monitor_utils import _game_dir_name, _normalize_slug_piece, _now_iso, _safe_float, _safe_int


class MLBPolymarketMonitor:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.tz = ZoneInfo(args.timezone)
        if args.date:
            self.date_str = args.date
        else:
            self.date_str = datetime.now(self.tz).strftime("%Y-%m-%d")

        self.output_root = args.output_root
        self.output_root.mkdir(parents=True, exist_ok=True)

        self.stats_client = MLBStatsClient(
            pitcher_cache_path=args.pitcher_cache_path,
            pitcher_cache_refresh_date=datetime.now(self.tz).strftime("%Y-%m-%d"),
        )
        self.discovery_client = PolymarketDiscoveryClient(timeout=args.gamma_timeout)
        self.book_client = PolymarketBookClient(timeout=args.clob_timeout)
        self.recorder = LocalRecorder(output_root=self.output_root, date_str=self.date_str)

        self.games: Dict[int, ScheduledGame] = {}
        self.matches: Dict[int, GameMarketMatch] = {}
        self.active_games: Dict[int, bool] = {}
        self.no_market_cache_until: Dict[int, float] = {}
        # Per (game_pk, line, side) failure controls for stale book retirement.
        self._book_fail_streak: Dict[Tuple[int, str, str], int] = {}
        self._book_fail_cooldown_until: Dict[Tuple[int, str, str], float] = {}
        self._book_fail_retire_count: Dict[Tuple[int, str, str], int] = {}
        self._book_fail_retire_log_count: Dict[Tuple[int, str, str], int] = {}
        self._book_fail_retire_rollup: Dict[Tuple[int, str, str], Dict[str, object]] = {}
        self._schedule_refresh_count = 0
        self._schedule_refresh_info_count = 0
        self._schedule_refresh_change_count = 0
        self._schedule_refresh_error_count = 0
        self._schedule_refresh_change_since_log = 0
        self._schedule_refresh_change_rollup: Counter[str] = Counter()
        self._last_schedule_signature: Optional[Tuple[int, int, int, Tuple[Tuple[int, str, str, Optional[int], Optional[int]], ...]]] = None
        self._market_discovery_no_new_cycles = 0
        self._market_discovery_no_new_attempted_games = 0
        self._market_discovery_no_new_since_log = 0
        self._market_discovery_no_new_attempted_since_log = 0
        self._tick_snapshots_written = 0
        self._poll_cycles = 0

        self.start_wall = time.time()
        self.stop_wall = self.start_wall + args.run_seconds if args.run_seconds > 0 else math.inf

        # Persistent thread pool: created once, reused every poll cycle.
        # Thread-local HTTP sessions in PolymarketBookClient stay alive between polls,
        # maintaining warm keep-alive connections to the CLOB (saves TCP handshake per cycle).
        self._executor = ThreadPoolExecutor(
            max_workers=args.max_workers,
            thread_name_prefix="monitor-poll",
        )
        # Pre-warm: force all threads to initialize now so the first poll cycle
        # pays zero thread-creation overhead (~1-5ms per thread × max_workers).
        def _noop(_idx: int) -> None:
            return None
        list(self._executor.map(_noop, range(args.max_workers)))

    @staticmethod
    def _book_fail_key(game_pk: int, line: str, side: str) -> Tuple[int, str, str]:
        return game_pk, str(line), side

    def _reset_book_failure_state(self, key: Tuple[int, str, str]) -> None:
        self._book_fail_streak.pop(key, None)
        self._book_fail_cooldown_until.pop(key, None)
        self._book_fail_retire_count.pop(key, None)
        self._book_fail_retire_log_count.pop(key, None)

    def _clear_book_failure_state_for_game(self, game_pk: int) -> None:
        keys = [k for k in self._book_fail_streak.keys() if k[0] == game_pk]
        keys.extend([k for k in self._book_fail_cooldown_until.keys() if k[0] == game_pk and k not in keys])
        keys.extend([k for k in self._book_fail_retire_count.keys() if k[0] == game_pk and k not in keys])
        for key in keys:
            self._reset_book_failure_state(key)

    def _is_book_poll_in_cooldown(self, key: Tuple[int, str, str], now: float) -> bool:
        return now < self._book_fail_cooldown_until.get(key, 0.0)

    def _is_retirable_book_failure(self, game: ScheduledGame, result: dict) -> bool:
        if result.get("ok"):
            return False
        err = str(result.get("error") or "").lower()
        status_code = result.get("status_code")
        is_gamma_invalid = err.startswith("gamma_invalid_price")
        is_404 = (
            status_code == 404
            or err.startswith("http_404")
            or err.startswith("gamma_http_404")
        )
        if is_gamma_invalid:
            return True
        if not is_404:
            return False

        # Avoid retiring pre-game "not deployed yet" 404s. Only treat 404 as
        # retirable when we are in a late, end-of-inning terminal-looking state.
        inning = game.score.inning
        inning_state = str(game.score.inning_state or "").strip().lower()
        outs = game.score.outs
        late_terminal_hint = (
            inning is not None
            and inning >= 9
            and inning_state == "end"
            and (outs is None or outs >= 3)
        )
        return late_terminal_hint

    def _record_retirable_book_failure(
        self,
        *,
        key: Tuple[int, str, str],
        game: ScheduledGame,
        line: str,
        side: str,
        token_id: str,
        result: dict,
        now: float,
    ) -> None:
        streak = self._book_fail_streak.get(key, 0) + 1
        self._book_fail_streak[key] = streak

        threshold = max(1, int(getattr(self.args, "book_failure_retire_streak", DEFAULT_BOOK_FAILURE_RETIRE_STREAK)))
        if streak < threshold:
            return

        retire_count = self._book_fail_retire_count.get(key, 0) + 1
        self._book_fail_retire_count[key] = retire_count

        base_cooldown = max(1.0, float(getattr(self.args, "book_failure_cooldown_secs", DEFAULT_BOOK_FAILURE_COOLDOWN_SECS)))
        max_cooldown = max(base_cooldown, float(getattr(self.args, "book_failure_max_cooldown_secs", DEFAULT_BOOK_FAILURE_MAX_COOLDOWN_SECS)))
        cooldown_secs = min(max_cooldown, base_cooldown * (2 ** (retire_count - 1)))
        self._book_fail_cooldown_until[key] = now + cooldown_secs
        self._book_fail_streak[key] = 0
        self._book_fail_retire_rollup[key] = {
            "game_pk": game.game_pk,
            "away_abbrev": game.away_abbrev,
            "home_abbrev": game.home_abbrev,
            "line": line,
            "side": side,
            "token_id": token_id,
            "retire_count": retire_count,
            "last_cooldown_secs": round(cooldown_secs, 3),
            "last_source": result.get("source"),
            "last_status": result.get("status_code"),
            "last_error": result.get("error"),
        }

        log_count = self._book_fail_retire_log_count.get(key, 0) + 1
        self._book_fail_retire_log_count[key] = log_count
        log_fn = LOGGER.info if log_count == 1 else LOGGER.debug
        log_fn(
            "Retiring book polling for %s@%s game=%d line=%s side=%s token=%s "
            "cooldown=%.0fs (retire_count=%d) after %d retirable failures | source=%s status=%s error=%s",
            game.away_abbrev,
            game.home_abbrev,
            game.game_pk,
            line,
            side,
            token_id[:12] + "..." if token_id else "",
            cooldown_secs,
            retire_count,
            threshold,
            result.get("source"),
            result.get("status_code"),
            result.get("error"),
        )

    @staticmethod
    def _schedule_signature(
        games: Dict[int, ScheduledGame],
    ) -> Tuple[int, int, int, Tuple[Tuple[int, str, str, Optional[int], Optional[int]], ...]]:
        final_count = sum(1 for g in games.values() if g.is_final())
        active_count = sum(1 for g in games.values() if g.is_live())
        compact = tuple(
            (
                int(game_pk),
                str(g.status_abstract or ""),
                str(g.score.inning_state or ""),
                g.score.away,
                g.score.home,
            )
            for game_pk, g in sorted(games.items())
        )
        return len(games), active_count, final_count, compact

    def _refresh_schedule(self) -> None:
        payload = self.stats_client.fetch_schedule_payload(self.date_str)
        self.recorder.write_schedule(payload)
        parsed = self.stats_client.parse_games(payload)
        self.games = parsed
        self._schedule_refresh_count += 1
        signature = self._schedule_signature(self.games)
        changed = signature != self._last_schedule_signature
        first_refresh = self._last_schedule_signature is None
        self._last_schedule_signature = signature
        active_count = signature[1]
        final_count = signature[2]
        if first_refresh:
            self._schedule_refresh_info_count += 1
            LOGGER.info(
                "Schedule refreshed for %s: %d games (%d live, %d final)",
                self.date_str,
                len(self.games),
                active_count,
                final_count,
            )
        elif changed:
            self._schedule_refresh_change_count += 1
            self._record_schedule_state_change_rollup(
                total_games=len(self.games),
                active_count=active_count,
                final_count=final_count,
            )
        elif self._schedule_refresh_count % SCHEDULE_UNCHANGED_DEBUG_EVERY_N_REFRESHES == 0:
            LOGGER.debug(
                "Schedule refreshed for %s: %d games (%d live, %d final; unchanged)",
                self.date_str,
                len(self.games),
                active_count,
                final_count,
            )

    def _record_schedule_state_change_rollup(
        self,
        *,
        total_games: int,
        active_count: int,
        final_count: int,
    ) -> None:
        """Compact frequent live-score schedule churn into periodic DEBUG rows."""
        self._schedule_refresh_change_since_log = int(
            getattr(self, "_schedule_refresh_change_since_log", 0) or 0
        ) + 1
        rollup = getattr(self, "_schedule_refresh_change_rollup", None)
        if rollup is None:
            rollup = Counter()
            self._schedule_refresh_change_rollup = rollup
        rollup[f"live={active_count}|final={final_count}"] += 1

        if self._should_log_schedule_state_change_debug():
            since_last = int(getattr(self, "_schedule_refresh_change_since_log", 0) or 0)
            top_states = ", ".join(
                f"{key}:{count}" for key, count in rollup.most_common(4)
            )
            LOGGER.debug(
                "Schedule state-change rollup for %s: changes_total=%d "
                "changes_since_last_log=%d games=%d live=%d final=%d states=%s",
                self.date_str,
                self._schedule_refresh_change_count,
                since_last,
                total_games,
                active_count,
                final_count,
                top_states or "none",
            )
            self._schedule_refresh_change_since_log = 0


    def _all_markets_deploying(self, game_pk: int) -> bool:
        """Return True if every O/U market for this game is still in deploying state."""
        match = self.matches.get(game_pk)
        if match is None:
            return False
        return bool(match.markets) and all(m.deploying for m in match.markets)

    def _discover_markets(self, now: float) -> None:
        discovered = 0
        attempted = 0
        slug_to_game_pk = {m.event_slug: gpk for gpk, m in self.matches.items()}
        for game_pk, game in sorted(self.games.items()):
            already_mapped = game_pk in self.matches
            # Re-check games where all markets are still deploying (not yet on CLOB).
            if already_mapped and not self._all_markets_deploying(game_pk):
                continue
            if game.is_final():
                continue
            next_retry = self.no_market_cache_until.get(game_pk, 0.0)
            if now < next_retry:
                continue
            attempted += 1
            match = self.discovery_client.discover_for_game(
                game=game,
                date_str=self.date_str,
                blocked_slugs=set(slug_to_game_pk.keys()) - ({self.matches[game_pk].event_slug} if already_mapped else set()),
            )
            if match is None:
                self.no_market_cache_until[game_pk] = now + 60.0
                continue
            # Check slug collision BEFORE inserting into self.matches.
            # Previously the match was inserted first, leaving a stale mapping
            # active even when the collision block triggered a continue.
            existing_game_pk = slug_to_game_pk.get(match.event_slug)
            if existing_game_pk is not None and existing_game_pk != game_pk:
                LOGGER.info(
                    "Skipping slug reuse %s for game %d (%s@%s); already mapped to game %d",
                    match.event_slug,
                    game_pk,
                    game.away_abbrev,
                    game.home_abbrev,
                    existing_game_pk,
                )
                self.no_market_cache_until[game_pk] = now + 300.0
                continue
            deploying_count = sum(1 for m in match.markets if m.deploying)
            ready_count = len(match.markets) - deploying_count
            if already_mapped and deploying_count == len(match.markets):
                # Still all deploying — back off and try again later.
                self.no_market_cache_until[game_pk] = now + 60.0
                LOGGER.debug(
                    "%s@%s (game %d): all %d markets still deploying, will recheck in 60s",
                    game.away_abbrev, game.home_abbrev, game_pk, deploying_count,
                )
                continue
            self.matches[game_pk] = match
            slug_to_game_pk[match.event_slug] = game_pk
            self.recorder.write_game_meta(game=game, match=match)
            discovered += 1
            if deploying_count:
                LOGGER.info(
                    "Mapped %s@%s (game %d) -> %s (%d O/U lines, %d ready, %d still deploying)",
                    game.away_abbrev, game.home_abbrev, game_pk,
                    match.event_slug, len(match.markets), ready_count, deploying_count,
                )
            else:
                LOGGER.info(
                    "Mapped %s@%s (game %d) -> %s (%d O/U lines)",
                    game.away_abbrev, game.home_abbrev, game_pk,
                    match.event_slug, len(match.markets),
                )
        if discovered or attempted:
            self.recorder.write_market_map(matches=self.matches, games=self.games)
        if attempted and not discovered:
            self._record_market_discovery_no_new(attempted)

    def _record_market_discovery_no_new(self, attempted: int) -> None:
        self._market_discovery_no_new_cycles += 1
        self._market_discovery_no_new_attempted_games += int(attempted)
        self._market_discovery_no_new_since_log += 1
        self._market_discovery_no_new_attempted_since_log += int(attempted)

        every = int(
            getattr(
                self,
                "_market_discovery_no_new_info_every_n_cycles",
                DISCOVERY_NO_NEW_INFO_EVERY_N_CYCLES,
            )
            or DISCOVERY_NO_NEW_INFO_EVERY_N_CYCLES
        )
        should_info = (
            self._market_discovery_no_new_cycles == 1
            or self._market_discovery_no_new_since_log >= max(1, every)
        )
        if should_info:
            LOGGER.info(
                "Market discovery no-new-mapping rollup: cycles_since_last=%d "
                "games_attempted_since_last=%d total_cycles=%d total_games_attempted=%d",
                self._market_discovery_no_new_since_log,
                self._market_discovery_no_new_attempted_since_log,
                self._market_discovery_no_new_cycles,
                self._market_discovery_no_new_attempted_games,
            )
            self._market_discovery_no_new_since_log = 0
            self._market_discovery_no_new_attempted_since_log = 0
        else:
            LOGGER.debug(
                "Market discovery attempted on %d games; no new mappings this cycle.",
                attempted,
            )

    def _should_poll_game(self, game: ScheduledGame) -> bool:
        if game.game_pk not in self.matches:
            return False
        if game.is_final():
            return False
        if self.args.start_on_preview:
            return game.is_live() or game.is_preview()
        return game.is_live()

    def _sync_active_games(self) -> None:
        for game_pk, game in list(self.games.items()):
            should_active = self._should_poll_game(game)
            currently_active = self.active_games.get(game_pk, False)
            if should_active and not currently_active:
                self.active_games[game_pk] = True
                deploying_count = sum(1 for m in self.matches[game_pk].markets if m.deploying) if game_pk in self.matches else 0
                ready_count = (len(self.matches[game_pk].markets) - deploying_count) if game_pk in self.matches else 0
                LOGGER.info(
                    "START monitoring game %d (%s@%s, state=%s) — %d lines ready, %d deploying",
                    game_pk,
                    game.away_abbrev,
                    game.home_abbrev,
                    game.status_abstract,
                    ready_count,
                    deploying_count,
                )
            elif not should_active and currently_active:
                self.active_games[game_pk] = False
                self.recorder.close_game(game_pk)
                self._clear_book_failure_state_for_game(game_pk)
                LOGGER.info(
                    "STOP monitoring game %d (%s@%s, state=%s)",
                    game_pk,
                    game.away_abbrev,
                    game.home_abbrev,
                    game.status_abstract,
                )

    def _build_poll_jobs(self) -> List[Tuple[ScheduledGame, OUMarket, str, str, int]]:
        jobs: List[Tuple[ScheduledGame, OUMarket, str, str, int]] = []
        now = time.time()
        for game_pk, on in sorted(self.active_games.items()):
            if not on:
                continue
            game = self.games.get(game_pk)
            match = self.matches.get(game_pk)
            if game is None or match is None:
                continue
            for market in match.markets:
                if market.deploying:
                    # CLOB book not yet live for this market; skip until re-discovery activates it.
                    continue
                # token_index 0=over/yes, 1=under/no — matches Gamma outcomePrices order.
                side_specs = (
                    ("over_yes", market.over_token_id, 0),
                    ("under_no", market.under_token_id, 1),
                )
                for side, token_id, token_index in side_specs:
                    key = self._book_fail_key(game.game_pk, market.line, side)
                    if self._is_book_poll_in_cooldown(key, now):
                        continue
                    jobs.append((game, market, side, token_id, token_index))
        return jobs

    def _poll_once(self) -> int:
        jobs = self._build_poll_jobs()
        if not jobs:
            # Keep subclass lifecycle hooks running even when there are no token books
            # to poll this cycle. Trading engines rely on this hook for settlement and
            # order lifecycle management (e.g., final-game settlement/cancellation).
            self._on_tick_batch([])
            return 0
        if self._should_log_poll_progress_debug():
            LOGGER.debug(
                "Polling %d token books (%d active games)",
                len(jobs),
                sum(1 for v in self.active_games.values() if v),
            )

        records_written = 0
        tick_batch: list = []
        # Persistent executor (created in __init__): no thread spawn/teardown per cycle,
        # thread-local HTTP sessions in PolymarketBookClient stay warm across polls.
        ex = self._executor
        futures = {}
        for game, market, side, token_id, token_index in jobs:
            fut = ex.submit(
                self.book_client.fetch_book,
                token_id,
                market.market_id,
                token_index,
            )
            futures[fut] = (game, market, side, token_id)
            if self.args.inter_request_delay > 0:
                time.sleep(self.args.inter_request_delay)

        for fut in as_completed(futures):
            game, market, side, token_id = futures[fut]
            result = fut.result()
            key = self._book_fail_key(game.game_pk, market.line, side)
            if result.get("ok"):
                self._reset_book_failure_state(key)
            else:
                if self._is_retirable_book_failure(game, result):
                    self._record_retirable_book_failure(
                        key=key,
                        game=game,
                        line=market.line,
                        side=side,
                        token_id=token_id,
                        result=result,
                        now=time.time(),
                    )
                else:
                    # Non-retirable errors (timeouts, transient upstream issues,
                    # pre-deploy states) should not accumulate stale-book retirement.
                    self._reset_book_failure_state(key)
            payload = {
                "ts": _now_iso(),
                "game_pk": game.game_pk,
                "away_abbrev": game.away_abbrev,
                "home_abbrev": game.home_abbrev,
                "side": side,
                "line": market.line,
                "market_id": market.market_id,
                "token_id": token_id,
                "question": market.question,
                "event_slug": self.matches[game.game_pk].event_slug if game.game_pk in self.matches else "",
                "game_status": game.status_abstract,
                "game_detailed_status": game.status_detailed,
                "inning": game.score.inning,
                "inning_state": game.score.inning_state,
                "outs": game.score.outs,
                "balls": game.score.balls,
                "strikes": game.score.strikes,
                "runners_on": game.score.runners_on,
                "away_score": game.score.away,
                "home_score": game.score.home,
                "away_inning_runs": list(game.score.away_inning_runs or []),
                "home_inning_runs": list(game.score.home_inning_runs or []),
                "book": result,
            }
            self.recorder.append_snapshot(game=game, line=market.line, side=side, payload=payload)
            tick_batch.append((game, market, side, payload))
            records_written += 1
        self._on_tick_batch(tick_batch)
        return records_written

    def _on_tick_batch(self, tick_batch: list) -> None:
        """Hook for subclasses. Called after every poll cycle with all tick payloads."""

    def _all_games_done(self) -> bool:
        if not self.games:
            return False
        return all(g.is_final() for g in self.games.values())

    def _log_monitor_rollup(self) -> None:
        """Emit one compact run summary so repeated DEBUG chatter stays auditable."""
        run_seconds = max(0.0, time.time() - self.start_wall)
        retired_total = sum(
            int(row.get("retire_count", 0) or 0)
            for row in self._book_fail_retire_rollup.values()
        )
        LOGGER.info(
            "=== MONITOR ROLLUP (%s) schedule_refreshes=%d changes=%d failures=%d "
            "poll_cycles=%d tick_snapshots=%d discovery_no_new_cycles=%d "
            "discovery_no_new_games_attempted=%d retired_book_keys=%d retire_events=%d runtime=%.0fs ===",
            self.date_str,
            self._schedule_refresh_count,
            self._schedule_refresh_change_count,
            self._schedule_refresh_error_count,
            self._poll_cycles,
            self._tick_snapshots_written,
            self._market_discovery_no_new_cycles,
            self._market_discovery_no_new_attempted_games,
            len(self._book_fail_retire_rollup),
            retired_total,
            run_seconds,
        )
        schedule_state_rollup = getattr(self, "_schedule_refresh_change_rollup", Counter())
        if schedule_state_rollup:
            top_states = ", ".join(
                f"{key}:{count}" for key, count in schedule_state_rollup.most_common(6)
            )
            LOGGER.info(
                "MONITOR SCHEDULE ROLLUP (%s) state_change_buckets=%s",
                self.date_str,
                top_states,
            )
        if self._book_fail_retire_rollup:
            top_rows = sorted(
                self._book_fail_retire_rollup.values(),
                key=lambda row: int(row.get("retire_count", 0) or 0),
                reverse=True,
            )[:8]
            for row in top_rows:
                token = str(row.get("token_id") or "")
                LOGGER.info(
                    "  retired_book %s@%s game=%s line=%s side=%s count=%s "
                    "last_cooldown=%.0fs source=%s status=%s error=%s token=%s",
                    row.get("away_abbrev"),
                    row.get("home_abbrev"),
                    row.get("game_pk"),
                    row.get("line"),
                    row.get("side"),
                    row.get("retire_count"),
                    float(row.get("last_cooldown_secs", 0.0) or 0.0),
                    row.get("last_source"),
                    row.get("last_status"),
                    row.get("last_error"),
                    token[:12] + "..." if token else "",
                )

    def _should_log_poll_progress_debug(self) -> bool:
        cycle = int(getattr(self, "_poll_cycles", 0) or 0)
        return (
            cycle <= 1
            or cycle % POLL_PROGRESS_DEBUG_EVERY_N_CYCLES == 0
        )

    def _should_log_schedule_state_change_debug(self) -> bool:
        changes = int(getattr(self, "_schedule_refresh_change_count", 0) or 0)
        return (
            changes <= 1
            or changes % SCHEDULE_CHANGED_DEBUG_EVERY_N_REFRESHES == 0
        )

    def run(self) -> None:
        LOGGER.info("Monitor date=%s tz=%s output=%s", self.date_str, self.args.timezone, self.output_root)
        _prevent_sleep()
        if self.args.performance_mode:
            _setup_performance_mode(self.args)
        next_schedule = 0.0
        next_discovery = 0.0
        next_poll = 0.0

        try:
            while True:
                now = time.time()
                if now >= self.stop_wall:
                    LOGGER.info("Reached --run-seconds limit; stopping.")
                    break

                if now >= next_schedule:
                    try:
                        self._refresh_schedule()
                    except Exception as exc:
                        self._schedule_refresh_error_count += 1
                        LOGGER.warning("Schedule refresh failed: %s", exc)
                    next_schedule = now + self.args.schedule_refresh_secs

                if now >= next_discovery:
                    try:
                        self._discover_markets(now=now)
                    except Exception as exc:
                        LOGGER.warning("Market discovery cycle failed: %s", exc)
                    next_discovery = now + self.args.discovery_refresh_secs

                self._sync_active_games()

                if now >= next_poll:
                    try:
                        self._poll_cycles += 1
                        n = self._poll_once()
                        self._tick_snapshots_written += n
                        if n > 0 and self._should_log_poll_progress_debug():
                            LOGGER.debug("Wrote %d tick snapshots", n)
                    except Exception as exc:
                        LOGGER.warning("Polling cycle failed: %s", exc)
                    next_poll = now + self.args.poll_interval

                if self.args.once:
                    LOGGER.info("--once set; exiting after first full cycle.")
                    break

                if self._all_games_done() and not any(self.active_games.values()):
                    # Final lifecycle callback before exit so trading subclasses can
                    # settle final bets/cancel orders even if next_poll has not yet
                    # fired after the last game transitioned to Final.
                    try:
                        self._on_tick_batch([])
                    except Exception as exc:
                        LOGGER.warning("Final tick hook before exit failed: %s", exc)
                    # 2026-05-22 (audit followup): if --date is in the past
                    # AND all games already final at the very first schedule
                    # poll, the operator almost certainly didn't mean to run
                    # on yesterday's games. Make the exit message explicit
                    # about the date mismatch instead of the bland "all
                    # final" line.
                    _today = datetime.now(self.tz).strftime("%Y-%m-%d")
                    if (
                        self.args.date
                        and self.args.date < _today
                        and self._poll_cycles <= 1
                    ):
                        LOGGER.warning(
                            "All scheduled games for --date=%s are already "
                            "final at the very first schedule fetch (today "
                            "is %s). This usually means --date is stale: "
                            "did you mean to run on TODAY'S games? Re-launch "
                            "without --date, or with --date %s. Exiting.",
                            self.args.date, _today, _today,
                        )
                    else:
                        LOGGER.info(
                            "All scheduled games are final and no active "
                            "monitors remain; exiting."
                        )
                    break

                time.sleep(0.4)
        finally:
            self._log_monitor_rollup()
            self.recorder.close_all()
            # Shut down the persistent thread pool gracefully.
            # wait=False: futures already collected in _poll_once via as_completed,
            # so there is no pending work to wait for at this point.
            self._executor.shutdown(wait=False)


def main() -> None:
    args = parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(message)s",
    )
    suppress_noisy_library_loggers()

    if args.date:
        datetime.strptime(args.date, "%Y-%m-%d")

    monitor = MLBPolymarketMonitor(args=args)
    try:
        monitor.run()
    except KeyboardInterrupt:
        LOGGER.info("Interrupted by user, shutting down.")


if __name__ == "__main__":
    main()


# --- Backward-compatibility re-exports ---------------------------------------
# Trading scripts (signal_engine, signal_pipeline, candidate_logging,
# capture_helpers, signal_config) and tests import a mix of constants, helpers,
# dataclasses, and clients directly from this module. Keep every legacy name
# importable here so the split is invisible to callers.
__all__ = [
    # constants
    "CLOB_BASE",
    "GAMMA_BASE",
    "MLB_SCHEDULE_URL",
    "DEFAULT_OUTPUT_ROOT",
    "DEFAULT_PITCHER_CACHE_PATH",
    "DEFAULT_TIMEZONE",
    "DEFAULT_BOOK_FAILURE_RETIRE_STREAK",
    "DEFAULT_BOOK_FAILURE_COOLDOWN_SECS",
    "DEFAULT_BOOK_FAILURE_MAX_COOLDOWN_SECS",
    "PITCHER_CACHE_MAX_AGE_HOURS",
    "PITCHER_CACHE_MIN_PITCHER_COUNT",
    "PITCHER_CACHE_STALE_FALLBACK_MAX_AGE_HOURS",
    "POLL_PROGRESS_DEBUG_EVERY_N_CYCLES",
    "SCHEDULE_UNCHANGED_DEBUG_EVERY_N_REFRESHES",
    "SCHEDULE_CHANGED_DEBUG_EVERY_N_REFRESHES",
    "DISCOVERY_NO_NEW_INFO_EVERY_N_CYCLES",
    "MLB_AVG_ERA",
    "TEAM_SLUGS",
    "LIVE_STATES",
    "PREVIEW_STATES",
    "FINAL_STATES",
    "OU_LINE_RE",
    "NOISY_LIBRARY_LOGGERS",
    "LOGGER",
    "logging",
    # utilities
    "_safe_float",
    "_safe_int",
    "_normalize_slug_piece",
    "_game_dir_name",
    "_now_iso",
    "suppress_noisy_library_loggers",
    # system
    "_setup_performance_mode",
    "_prevent_sleep",
    # CLI
    "parse_args",
    # dataclasses
    "ScheduleScore",
    "ScheduledGame",
    "OUMarket",
    "GameMarketMatch",
    # clients + recorder
    "LocalRecorder",
    "MLBStatsClient",
    "PolymarketDiscoveryClient",
    "PolymarketBookClient",
    # orchestrator + entry
    "MLBPolymarketMonitor",
    "main",
]
