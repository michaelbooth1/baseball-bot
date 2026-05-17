#!/usr/bin/env python3
"""
line_state.py -- Per-(game,line) rolling state and small pure helpers.

Extracted from signal_engine.py (Tier 2 refactor, 2026-05-01). The
LineState dataclass plus the small module-level helpers (_mid, _now_iso,
_now_ts, _runs_pace_ok, _ask_edge_boost) have no engine coupling and lift
out cleanly. signal_engine re-exports LineState and the timestamp helpers
so existing `from signal_engine import LineState, _now_iso, _now_ts`
imports (live_engine, paper_trader, tests) keep working.

LineState owns:
  - ask_history:               last 200 ask prints, basis for jump detection
  - tick_buffer:               last 120 (ts, bid, ask, mid, spread) snapshots
                                 for Family C velocity / drift features
  - baseline_ask:              committed pre-event price baseline
  - pending_signal state:      multi-tick confirmation gating per [TR2]
  - cooldown_remaining:        post-bet cooldown counter
  - bet_open:                  whether a bet is currently active for this line
  - score_segment_*:           same-score segment tracking for shadow no-score
                                 drift candidates (observability only, never
                                 changes score-event gate behavior)
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Deque, Dict, Optional, Tuple

from signal_config import (
    CONFIRM_RETAIN_FRACTION,
    DEFAULT_COOLDOWN_TICKS,
    DEFAULT_LOOKBACK,
    DEFAULT_STABLE_WINDOW,
)


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

def _mid(book: dict) -> Optional[float]:
    bid = book.get("best_bid")
    ask = book.get("best_ask")
    if bid is not None and ask is not None:
        return (bid + ask) / 2.0
    if bid is not None:
        return bid
    if ask is not None:
        return ask
    return book.get("ltp")


def _now_iso() -> str:
    return datetime.utcnow().isoformat() + "Z"


def _now_ts() -> float:
    return time.time()


def _runs_pace_ok(current_total: int, inning: int, line: str) -> bool:
    """[TR3] Check scoring pace can plausibly support reaching the line.

    Formula: (current_total / inning) * 9 >= float(line) - 1.5

    A 1.5-run buffer accounts for late-inning acceleration (bullpen changes,
    close-game situational hitting). Inning 0 is guarded against divide-by-zero.
    """
    if inning <= 0:
        return True  # too early to measure pace -- don't block
    pace_per_9 = (current_total / inning) * 9
    required = float(line) - 1.5
    return pace_per_9 >= required


def _ask_edge_boost(
    *,
    ask: float,
    start: float,
    end: float,
    max_boost: float,
) -> float:
    """Return additional edge requirement for high ask regimes."""
    if ask <= start:
        return 0.0
    if end <= start:
        return max(0.0, max_boost)
    ratio = (ask - start) / (end - start)
    ratio = min(max(ratio, 0.0), 1.0)
    return max(0.0, max_boost) * ratio


# ---------------------------------------------------------------------------
# Per-line state tracker
# ---------------------------------------------------------------------------

@dataclass
class LineState:
    """Rolling state for one game+line combination."""
    game_pk: int
    line: str

    # [TR2] Track ask history separately from mid (Change 2)
    ask_history: Deque[float] = field(default_factory=lambda: deque(maxlen=200))

    # [Family C] Per-tick (ts, bid, ask, mid, spread) buffer for velocity / drift
    # features. Maxlen 120 covers ~5 minutes at 2.5s monitor cadence -- well over
    # the 30s window any current Family C feature needs.
    tick_buffer: Deque[Dict[str, float]] = field(default_factory=lambda: deque(maxlen=120))

    # Committed baseline: ask value accepted as "pre-event" price
    baseline_ask: Optional[float] = None
    stable_count: int = 0
    baseline_candidate: Optional[float] = None

    # [TR2] Pending signal confirmation state (Change 1)
    # When a jump is first detected, we wait confirmation_ticks more ticks
    # before firing. If ask drops back, the pending signal is cancelled.
    pending_signal: bool = False
    pending_ticks_remaining: int = 0
    pending_jump_ask: Optional[float] = None  # ask value at first detection

    # Cooldown after placing a bet
    cooldown_remaining: int = 0

    # Whether a bet is already open for this game+line
    bet_open: bool = False

    # Shadow state-value research: same-score segment drift tracking.
    # This is observability only. It lets signal_pipeline log compact no-score
    # drift candidates without changing score-event gate behavior.
    score_segment_key: Optional[Tuple[int, int]] = None
    score_segment_started_at: Optional[float] = None
    score_segment_high_ask: Optional[float] = None
    score_segment_ticks: int = 0
    score_segment_shadow_logged: bool = False

    STABLE_TOL: float = 0.02  # +/- 2 cents = "stable"

    def push_ask(self, ask: float) -> None:
        """Update ask history and baseline candidate. Called every tick."""
        self.ask_history.append(ask)
        if self.cooldown_remaining > 0:
            self.cooldown_remaining -= 1
            return

        # Pending confirmation countdown
        if self.pending_signal:
            # Require the ask to retain at least CONFIRM_RETAIN_FRACTION of the
            # original jump above baseline. Prevents a large spike that partially
            # reverts from still confirming on near-zero residual.
            original_jump = (
                (self.pending_jump_ask - self.baseline_ask)
                if (self.baseline_ask is not None and self.pending_jump_ask is not None)
                else 0.0
            )
            min_ask_for_confirm = (
                self.baseline_ask + original_jump * CONFIRM_RETAIN_FRACTION
                if self.baseline_ask is not None
                else None
            )
            if min_ask_for_confirm is not None and ask >= min_ask_for_confirm:
                self.pending_ticks_remaining -= 1
                # Don't update baseline while pending
                return
            else:
                # Ask dropped below confirmation threshold -- cancel pending signal
                self.pending_signal = False
                self.pending_ticks_remaining = 0
                self.pending_jump_ask = None
                # Fall through to baseline update

        # Baseline candidate tracking
        if self.baseline_candidate is None:
            self.baseline_candidate = ask
            self.stable_count = 1
        elif abs(ask - self.baseline_candidate) <= self.STABLE_TOL:
            self.stable_count += 1
            if self.stable_count >= DEFAULT_STABLE_WINDOW:
                self.baseline_ask = self.baseline_candidate
        else:
            # Price moved -- reset candidate
            self.baseline_candidate = ask
            self.stable_count = 1

    def push_tick(
        self,
        ts: float,
        bid: Optional[float],
        ask: Optional[float],
    ) -> None:
        """[Family C] Append one timestamped book snapshot to the velocity buffer.

        Called every monitor tick alongside push_ask. No side effects on
        baseline / cooldown / pending-signal logic; buffer is read-only
        state for downstream feature computation.

        Skips entries with missing bid or ask so consumers don't have to
        re-filter; mid and spread are pre-computed at insert time.
        """
        if bid is None or ask is None:
            return
        try:
            b = float(bid)
            a = float(ask)
        except (TypeError, ValueError):
            return
        self.tick_buffer.append({
            "ts": float(ts),
            "bid": b,
            "ask": a,
            "mid": (b + a) / 2.0,
            "spread": a - b,
        })

    def ask_jump(self, lookback: int = DEFAULT_LOOKBACK) -> Optional[float]:
        """[TR2 Change 2] Return ask change over last `lookback` ticks."""
        hist = list(self.ask_history)
        if len(hist) < lookback + 1:
            return None
        return hist[-1] - hist[-(lookback + 1)]

    def is_confirmed_signal(self, jump_threshold: float, confirmation_ticks: int,
                            lookback: int = DEFAULT_LOOKBACK) -> bool:
        """[TR2 Change 1] Return True when a sustained jump is confirmed."""
        if self.pending_signal:
            return self.pending_ticks_remaining <= 0
        # Not yet in pending -- check if this is a new jump
        jump = self.ask_jump(lookback)
        if jump is not None and jump >= jump_threshold and self.baseline_ask is not None:
            if confirmation_ticks <= 1:
                return True
            # Start pending countdown
            self.pending_signal = True
            self.pending_ticks_remaining = confirmation_ticks - 1
            self.pending_jump_ask = list(self.ask_history)[-1]
            return False
        return False

    def reset_after_bet(self) -> None:
        self.cooldown_remaining = DEFAULT_COOLDOWN_TICKS
        self.baseline_ask = None
        self.baseline_candidate = None
        self.stable_count = 0
        self.pending_signal = False
        self.pending_ticks_remaining = 0
        self.pending_jump_ask = None
        self.bet_open = True

    def update_score_segment(self, *, away_score: int, home_score: int, ask: float, now: float) -> None:
        """Track high-water ask within the current same-score segment."""
        key = (int(away_score), int(home_score))
        if self.score_segment_key != key:
            self.score_segment_key = key
            self.score_segment_started_at = float(now)
            self.score_segment_high_ask = float(ask)
            self.score_segment_ticks = 1
            self.score_segment_shadow_logged = False
            return

        self.score_segment_ticks += 1
        if self.score_segment_high_ask is None:
            self.score_segment_high_ask = float(ask)
        else:
            self.score_segment_high_ask = max(float(self.score_segment_high_ask), float(ask))
