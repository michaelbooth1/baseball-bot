#!/usr/bin/env python3
"""
live_engine.py -- Live trading engine for MLB Polymarket O/U markets.

Extends SignalEngine. Gate logic, signal detection, the FV model stack
(Stage-1/2/3), and score-event decision order are inherited from the shared
pipeline. This file owns the live execution layer: fresh-book pricing, CLOB
order placement, budget exposure, order polling, cancellation, fill recovery,
and live session/ledger persistence.

Architecture:
  SignalEngine   (signal_engine.py)     -- all model and gate logic
      +-- LiveTradingEngine (this file) -- order execution layer only
              +-- CLOBOrderClient       -- CLOB auth + API calls (polymarket_client.py)

Order lifecycle per bet:
  1. Signal fires (inherited gate pipeline)
  2. _place_bet() called -> compute limit price -> CLOBOrderClient.place_limit_buy()
  3. order_id stored in LiveBetRecord + appended to live_orders_ledger.jsonl
  4. Every tick: _check_open_orders() polls CLOB for fills
  5. Filled -> settle the LiveBetRecord (P&L from actual fill shares and price)
  6. FV decays to within FV_CANCEL_MIN_EDGE of limit -> cancel + record as missed

Sizing invariant:
  Desired stake is USDC notional. The CLOB limit order size is shares, computed
  as stake_usdc / limit_price. Settlement uses cost = shares * fill_price and
  payout = shares if the Over wins.

Limit price formula (calibrated from book capture data):
    limit_price = bid + spread * spread_factor
    cap:   limit_price <= fv - min_edge   (preserve model edge)
    cap:   limit_price <= ask - 0.01      (must be inside spread)
    floor: limit_price >= bid + 0.01      (must improve on bid)
    round to 2dp (Polymarket minimum tick)

Safety controls:
  --dry-run             Full logic, logs orders but does NOT post to CLOB
  --max-open-orders N   Block new bets when N orders are unresolved (default 5)
  --order-timeout-secs  Cancel unfilled orders after this many seconds (default 10800)
  --spread-factor       Limit price position in spread (default 0.65)

Usage:
    python live_engine.py
    python live_engine.py --dry-run
    python live_engine.py --stake 50 --spread-factor 0.60
    python live_engine.py --max-open-orders 3 --order-timeout-secs 180
"""

from __future__ import annotations

import argparse
import json
import logging
import subprocess
import sys
import time
from dataclasses import asdict, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Project path setup
# ---------------------------------------------------------------------------

PROJECT_DIR = Path(__file__).resolve().parents[2]

sys.path.insert(0, str(PROJECT_DIR / "scripts" / "monitor"))
sys.path.insert(0, str(PROJECT_DIR / "scripts" / "analysis"))
sys.path.insert(0, str(PROJECT_DIR / "cache"))

# Signal engine provides all gate logic, FV model, and simulation bet placement
from signal_engine import (  # noqa: E402
    SignalEngine,
    BetRecord,
    LineState,
    parse_trade_args,
    DEFAULT_ASK_EDGE_RAMP_ENABLED,
    DEFAULT_ASK_EDGE_RAMP_START,
    DEFAULT_ASK_EDGE_RAMP_END,
    DEFAULT_ASK_EDGE_RAMP_MAX_BOOST,
    DEFAULT_BLOWOUT_RELAX_MAX_INNING,
    DEFAULT_BLOWOUT_RELAX_MIN_ASK,
    DEFAULT_BLOWOUT_RELAX_MAX_RUNS_NEEDED,
    DEFAULT_GATE_MIN_CURRENT_TOTAL_RELAX_MODE,
    DEFAULT_GATE_BLOWOUT_RELAX_MODE,
    DEFAULT_GATE_RELAX_AB_FRACTION,
    DEFAULT_MIN_CURRENT_TOTAL_RELAX_MAX_LEAD,
    DEFAULT_MIN_CURRENT_TOTAL_RELAX_MAX_RUNS_NEEDED,
    DEFAULT_PROB_CALIBRATION_MODE,
    DEFAULT_SHADOW_NO_SCORE_DRIFT_ENABLED,
    DEFAULT_SHADOW_NO_SCORE_DRIFT_MIN_INNING,
    DEFAULT_SHADOW_NO_SCORE_DRIFT_MAX_INNING,
    DEFAULT_SHADOW_NO_SCORE_DRIFT_MIN_SEGMENT_AGE_SECS,
    DEFAULT_SHADOW_NO_SCORE_DRIFT_MIN_SEGMENT_TICKS,
    DEFAULT_SHADOW_NO_SCORE_DRIFT_MIN_DRAWDOWN,
    DEFAULT_SHADOW_NO_SCORE_DRIFT_MIN_ASK,
    DEFAULT_SHADOW_NO_SCORE_DRIFT_MAX_ASK,
    DEFAULT_SHADOW_NO_SCORE_DRIFT_MAX_SPREAD,
    DEFAULT_SHADOW_NO_SCORE_DRIFT_MIN_PO_EDGE,
    DEFAULT_SHADOW_NO_SCORE_DRIFT_MIN_EMP_EDGE,
    DEFAULT_STABLE_WINDOW,
    DEFAULT_COOLDOWN_TICKS,
    _inning_state_to_half,
    _now_iso,
    _now_ts,
    LOGGER as SIGNAL_LOGGER,
)
from polymarket_client import CLOBOrderClient  # noqa: E402
from models import LiveBetRecord  # noqa: E402
from session_serialization import (  # noqa: E402
    build_live_session_payload as _build_live_session_payload,
)
from live_pricing import (  # noqa: E402
    compute_limit_price as _compute_limit_price_impl,
    compute_stake as _compute_stake_impl,
    filled_notional as _filled_notional_impl,
    kelly_components as _kelly_components_impl,
)
from live_ev_policy_runtime import (  # noqa: E402
    build_ev_feature_row as _build_ev_feature_row_impl,
    evaluate_ev_policy as _evaluate_ev_policy_impl,
    load_ev_policy_runtime as _load_ev_policy_runtime_impl,
)
from live_diagnostics import (  # noqa: E402
    build_current_state_edge_band_diagnostics as _build_current_state_edge_band_diagnostics_impl,
    build_shadow_feature_diagnostics as _build_shadow_feature_diagnostics_impl,
    build_shadow_order_diagnostics as _build_shadow_order_diagnostics_impl,
    current_state_edge_band as _current_state_edge_band_impl,
    log_current_state_edge_band_diagnostics as _log_current_state_edge_band_diagnostics_impl,
    log_shadow_feature_diagnostics as _log_shadow_feature_diagnostics_impl,
    log_shadow_order_diagnostics as _log_shadow_order_diagnostics_impl,
)
from live_session_loading import (  # noqa: E402
    load_existing_session as _load_existing_session_impl,
)
from live_order_lifecycle import (  # noqa: E402
    cancel_orders_for_game as _cancel_orders_for_game_impl,
    cancel_stale_orders as _cancel_stale_orders_impl,
    check_ask_reversal as _check_ask_reversal_impl,
    check_fv_decay as _check_fv_decay_impl,
    check_open_orders as _check_open_orders_impl,
    recompute_fv as _recompute_fv_impl,
    release_line_after_unfilled_order as _release_line_after_unfilled_order_impl,
    try_recover_fill as _try_recover_fill_impl,
)
from live_reconciliation import (  # noqa: E402
    reconcile_orphan_fills as _reconcile_orphan_fills_impl,
)
from live_engine_session_io import (  # noqa: E402
    append_to_ledger as _append_to_ledger_impl,
    append_to_live_ledger as _append_to_live_ledger_impl,
    bootstrap_live_ledger_event_keys as _bootstrap_live_ledger_event_keys_impl,
    live_bet_event_type as _live_bet_event_type_impl,
    live_ledger_event_key as _live_ledger_event_key_impl,
    save_session as _save_session_impl,
    write_live_ledger_row as _write_live_ledger_row_impl,
)
from live_engine_placement import (  # noqa: E402
    place_bet as _place_bet_impl,
)
from order_status import (  # noqa: E402
    EXPOSURE_COUNTED_ORDER_STATUSES,
    is_exposure_counted_status as _is_exposure_counted_status,
    normalize_accepted_order_status as _normalize_accepted_order_status,
    normalize_order_status as _normalize_order_status,
)
from build_daily_human_review_report import (  # noqa: E402
    DEFAULT_OUTPUT_ROOT as _DAILY_HUMAN_REVIEW_OUTPUT_ROOT,
    build_report as _build_daily_human_review_report,
    write_report as _write_daily_human_review_report,
)
from build_calibration_opportunity_training_table import (  # noqa: E402
    main as _refresh_calibration_opportunity_training_table,
)
from build_model_maturity_report import (  # noqa: E402
    DEFAULT_OUTPUT_ROOT as _MODEL_MATURITY_OUTPUT_ROOT,
    build_report as _build_model_maturity_report,
    write_report as _write_model_maturity_report,
)
from run_daily_refresh import (  # noqa: E402
    RefreshConfig as _StartupRefreshConfig,
    run_startup_refresh as _run_startup_refresh_impl,
)

LOGGER = logging.getLogger("live_engine")

NOISY_LIBRARY_LOGGERS = (
    "urllib3",
    "urllib3.connectionpool",
    "httpcore",
    "httpcore.connection",
    "httpcore.http11",
    "httpcore.http2",
    "httpx",
    "hpack",
    "hpack.hpack",
    "hpack.table",
    "rlp",
    "rlp.codec",
    "web3",
    "eth_account",
)

# Backward-compatible aliases
PaperTradingEngine = SignalEngine
PaperBet = BetRecord
LiveBet = LiveBetRecord

# ---------------------------------------------------------------------------
# CLI defaults + argparse parser live in live_engine_cli.py; re-exported here
# so existing imports (`from live_engine import DEFAULT_DAILY_BUDGET`, etc.)
# keep working without churn.
# ---------------------------------------------------------------------------

from live_engine_cli import (  # noqa: E402
    DEFAULT_SPREAD_FACTOR,
    DEFAULT_ORDER_TIMEOUT_SECS,
    DEFAULT_MAX_OPEN_ORDERS,
    DEFAULT_MIN_ORDER_SIZE,
    DEFAULT_ENV_FILE,
    DEFAULT_EV_POLICY_REPORT_PATH,
    DEFAULT_EV_WIN_MODEL_PATH,
    DEFAULT_EV_FILL_MODEL_PATH,
    DEFAULT_LIVE_STAKE,
    DEFAULT_DAILY_BUDGET,
    DEFAULT_PER_GAME_BUDGET_FRACTION,
    DEFAULT_MAX_CORRELATED_OVER_LINES_PER_GAME,
    DEFAULT_MIN_CORRELATED_LINE_GAP,
    DEFAULT_STAKE_MODE,
    DEFAULT_KELLY_FRACTION,
    DEFAULT_KELLY_MAX_BET_FRACTION,
    DEFAULT_KELLY_MAX_EDGE,
    DEFAULT_KELLY_FLOOR_TO_MIN,
    DEFAULT_SESSION_SAVE_MIN_INTERVAL_SECS,
    DEFAULT_WAIT_FOR_CLOB_TIMEOUT_SECS,
    DEFAULT_WAIT_FOR_CLOB_POLL_SECS,
    DEFAULT_STARTUP_REFRESH_ENABLED,
    DEFAULT_STARTUP_WEATHER_PROVIDER,
    DEFAULT_STARTUP_WEATHER_TIMEOUT_SECS,
    FV_CANCEL_MIN_EDGE,
    DEFAULT_FV_DECAY_MIN_AGE_SECS,
    DEFAULT_FV_DECAY_MIN_ASK_DROP,
    DEFAULT_ASK_REVERSAL_DROP,
    DEFAULT_ASK_REVERSAL_WINDOW,
    DEFAULT_ASK_REVERSAL_WINDOW_BUFFER,
    parse_live_args,
)

CURRENT_STATE_EDGE_DANGER_THRESHOLD = 0.03  # shadow diagnostic only, never a gate
CURRENT_STATE_EDGE_STRONG_THRESHOLD = 0.08  # shadow diagnostic only, never a gate

# How often (in poll cycles) to check open order fill status.
ORDER_POLL_EVERY_N_CYCLES = 2

# Live output directories
DEFAULT_LIVE_ROOT = PROJECT_DIR / "data" / "live_trading"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _safe_float(v: Any) -> Optional[float]:
    try:
        if v is None or v == "":
            return None
        return float(v)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# LiveTradingEngine
# ---------------------------------------------------------------------------

class LiveTradingEngine(SignalEngine):
    """
    Live-trading engine -- inherits all model and gate logic from SignalEngine
    and overrides _place_bet() to place real limit orders on the Polymarket CLOB.
    """

    def __init__(self, args: argparse.Namespace, trade_args: argparse.Namespace,
                 live_args: argparse.Namespace):
        # Redirect output paths to live_trading/ before calling parent init
        self._live_root = DEFAULT_LIVE_ROOT
        self._live_root.mkdir(parents=True, exist_ok=True)
        (self._live_root / "sessions").mkdir(exist_ok=True)
        (self._live_root / "book_captures").mkdir(exist_ok=True)
        (self._live_root / "candidate_universe").mkdir(exist_ok=True)

        # Patch paper_root so parent writes to live_trading/ not paper_trading/
        trade_args.paper_root = self._live_root

        # Active #8 prep (2026-05-17): bridge live-only CLI flags onto
        # trade_args so SignalEngine.__init__ can read them via the
        # standard getattr(trade_args, ..., default) pattern. This is
        # the explicit alternative to the implicit "add to both parsers"
        # approach; it keeps the runtime contract clean (engine reads
        # only from trade_args) without duplicating flag registration.
        trade_args.stage1_shadow_empirical_mode = getattr(
            live_args, "stage1_shadow_empirical_override", "off",
        )

        super().__init__(args=args, trade_args=trade_args)

        self.live_args = live_args
        self._candidate_mode = "live"

        # Live-specific ledger paths
        self._live_ledger_path = self._live_root / "master_ledger.jsonl"
        self._live_orders_path = self._live_root / "live_orders_ledger.jsonl"
        self._live_session_path = (
            self._live_root / "sessions" / f"{self.date_str}_session.json"
        )
        self._live_ledger_event_keys_written: set[Tuple[str, str, str]] = set()
        self._live_ledger_event_keys_bootstrapped: bool = False
        # Override parent session path
        self._session_path = self._live_session_path

        # EV policy (optional)
        self._ev_policy_mode: str = getattr(live_args, "ev_policy_mode", "off")
        self._ev_policy_runtime: Optional[Dict] = None
        self._ev_policy_stats: Dict[str, int] = {
            "scored": 0,
            "shadow_allow": 0,
            "shadow_block": 0,
            "enforce_allow": 0,
            "enforce_block": 0,
            "missing_runtime_features": 0,
        }
        if self._ev_policy_mode == "enforce":
            LOGGER.warning(
                "EV policy ENFORCE mode requested. Current research posture is "
                "stable filters plus EV policy shadow diagnostics; use enforce "
                "only for an explicit risk-approved run."
            )
        if self._ev_policy_mode in ("shadow", "enforce"):
            self._load_ev_policy_runtime()

        # Dry-run mode
        self._dry_run = bool(getattr(live_args, "dry_run", False))

        # CLOB client. Deposit-wallet (ERC-1271 / sig_type=3) is opt-in via
        # --use-deposit-wallet CLI flag; when not passed the client falls back
        # to POLY_USE_DEPOSIT_WALLET / POLY_DEPOSIT_WALLET env vars and finally
        # to the legacy proxy (sig_type=2) path.
        cli_use_deposit_wallet = getattr(live_args, "use_deposit_wallet", False)
        cli_deposit_wallet = getattr(live_args, "deposit_wallet", None)
        self._clob = CLOBOrderClient.from_env(
            env_path=getattr(live_args, "env_file", DEFAULT_ENV_FILE),
            dry_run=self._dry_run,
            use_deposit_wallet=True if cli_use_deposit_wallet else None,
            deposit_wallet=cli_deposit_wallet,
        )
        self._clob.initialize()

        # Wait for CLOB availability if requested (e.g. during maintenance)
        if getattr(live_args, "wait_for_clob", False):
            self._wait_for_clob_ready()

        # Open orders tracking: {order_id: LiveBetRecord}
        self._open_orders: Dict[str, LiveBetRecord] = {}
        self._poll_cycle_count = 0
        self._last_place_bet_skip_reason: Optional[str] = None

        # Wallet-aware paper fallback state (shipped 2026-05-13).
        # When CLOB rejects with "not enough balance / allowance", the
        # engine trips a session-level cooldown so subsequent placements
        # skip the CLOB and synthesize a paper-fallback bet instead. Real
        # money resumes automatically when the cooldown elapses.
        self._wallet_exhausted_until: Optional[float] = None  # monotonic ts
        self._paper_fallback_stats: Dict[str, Any] = {
            "placed": 0,
            "wins": 0,
            "losses": 0,
            "profit": 0.0,
            "total_stake": 0.0,
            "wallet_exhausted_events": 0,
            "wallet_exhausted_last_at": None,
            "last_reason": None,
        }

        # Session save throttling (reduce repeated full JSON rewrites during bursty updates).
        self._session_save_min_interval_secs = max(
            0.0,
            float(
                getattr(
                    live_args,
                    "session_save_min_interval_secs",
                    DEFAULT_SESSION_SAVE_MIN_INTERVAL_SECS,
                )
            ),
        )
        self._last_session_save_ts: float = 0.0
        self._session_save_pending: bool = False
        self._shadow_order_summary_logged: bool = False
        self._shadow_feature_summary_logged: bool = False
        self._current_state_edge_band_summary_logged: bool = False
        self._daily_human_review_written: bool = False
        self._model_maturity_report_written: bool = False

        dry_tag = " [DRY RUN]" if self._dry_run else ""

        stake_mode = getattr(live_args, "stake_mode", DEFAULT_STAKE_MODE)
        if stake_mode == "kelly":
            kelly_frac = getattr(live_args, "kelly_fraction", DEFAULT_KELLY_FRACTION)
            kelly_cap  = getattr(live_args, "kelly_max_bet_fraction", DEFAULT_KELLY_MAX_BET_FRACTION)
            kelly_edge_cap = getattr(live_args, "kelly_max_edge", DEFAULT_KELLY_MAX_EDGE)
            kelly_floor = bool(getattr(live_args, "kelly_floor_to_min", DEFAULT_KELLY_FLOOR_TO_MIN))
            stake_desc = (
                f"kelly (f={kelly_frac:.2f}, cap={kelly_cap:.0%} of budget, "
                f"edge_cap={kelly_edge_cap:.2f}, floor_to_min={kelly_floor})"
            )
        else:
            stake_desc = f"flat ${getattr(trade_args, 'stake', DEFAULT_LIVE_STAKE):.0f}"

        LOGGER.info(
            "LiveTradingEngine initialized%s | stake=%s | budget=$%.0f | "
            "ev_policy=%s | fv_cancel_min_edge=%.2f | fv_decay_min_age=%.0fs",
            dry_tag, stake_desc,
            getattr(live_args, "daily_budget", DEFAULT_DAILY_BUDGET),
            self._ev_policy_mode,
            getattr(live_args, "fv_cancel_min_edge", FV_CANCEL_MIN_EDGE),
            getattr(live_args, "fv_decay_min_age_secs", DEFAULT_FV_DECAY_MIN_AGE_SECS),
        )

        # Resume from today's session if restarting mid-day
        self._load_existing_session()

        # If a prior session crashed before its shutdown reconciliation ran,
        # any orphan fills from yesterday/today will still be sitting in the
        # ledger as `cancelled`/`missed`. Sweep once on startup so the runtime
        # state matches the wallet from the first tick.
        if not self._dry_run:
            self._reconcile_orphan_fills()

    # ------------------------------------------------------------------
    # Session resume
    # ------------------------------------------------------------------

    def _load_existing_session(self) -> None:
        return _load_existing_session_impl(self)

    # ------------------------------------------------------------------
    # CLOB availability wait (for maintenance/rollout windows)
    # ------------------------------------------------------------------

    def _wait_for_clob_ready(self) -> None:
        """Block until the CLOB API responds to a health check.

        Used with --wait-for-clob to survive scheduled downtime windows
        (e.g. the 2026-05-04 deposit wallet rollout). Polls with fixed
        intervals and exits after timeout.
        """
        timeout = getattr(
            self.live_args, "wait_for_clob_timeout_secs",
            DEFAULT_WAIT_FOR_CLOB_TIMEOUT_SECS,
        )
        poll_interval = DEFAULT_WAIT_FOR_CLOB_POLL_SECS
        start = time.time()

        if self._clob.health_check():
            LOGGER.info("CLOB health check passed -- ready to trade.")
            return

        LOGGER.warning(
            "CLOB is unreachable. Waiting up to %.0fs for it to come back "
            "(poll every %.0fs)...",
            timeout, poll_interval,
        )

        while True:
            elapsed = time.time() - start
            if elapsed >= timeout:
                LOGGER.error(
                    "CLOB did not become available within %.0fs. "
                    "Proceeding anyway -- orders will fail until CLOB recovers.",
                    timeout,
                )
                return

            time.sleep(poll_interval)

            if self._clob.health_check():
                LOGGER.info(
                    "CLOB is back online after %.0fs. Ready to trade.",
                    time.time() - start,
                )
                return

            LOGGER.info(
                "CLOB still unreachable (%.0fs / %.0fs elapsed)...",
                elapsed + poll_interval, timeout,
            )

    # ------------------------------------------------------------------
    # EV policy
    # ------------------------------------------------------------------

    def _load_ev_policy_runtime(self) -> None:
        return _load_ev_policy_runtime_impl(self)

    def _build_ev_feature_row(
        self,
        game,
        market,
        line_val: float,
        best_ask: float,
        bid: float,
        fair_value: float,
        base_fair_value: float,
        stage2_run_env_delta: float,
        team_offense_delta: float,
        edge: float,
        inferred_runs: int,
        inning: int,
        inning_state: str,
        outs: int,
        away_score_before: int,
        home_score_before: int,
        runners_on: int,
        limit_price: float,
        stake: float,
        ltp: Optional[float] = None,
        execution_book: Optional[Dict[str, Any]] = None,
        state_value_diagnostics: Optional[Dict[str, object]] = None,
    ) -> Dict[str, Any]:
        return _build_ev_feature_row_impl(
            self, game, market, line_val, best_ask, bid, fair_value,
            base_fair_value, stage2_run_env_delta, team_offense_delta, edge,
            inferred_runs, inning, inning_state, outs, away_score_before,
            home_score_before, runners_on, limit_price, stake,
            ltp=ltp,
            execution_book=execution_book,
            state_value_diagnostics=state_value_diagnostics,
        )

    def _evaluate_ev_policy(
        self, feature_row: Dict[str, Any], stake: float, price: float
    ) -> Tuple[bool, Dict[str, Any]]:
        return _evaluate_ev_policy_impl(self, feature_row, stake, price)

    # ------------------------------------------------------------------
    # Graceful shutdown
    # ------------------------------------------------------------------

    def _shutdown_gracefully(self) -> None:
        """Cancel all open orders and save final session state before exiting.

        Called on SIGINT/SIGTERM. Attempts fill recovery for each order
        before cancelling to avoid missing a late fill.
        """
        if self._open_orders:
            LOGGER.info(
                "Graceful shutdown: cancelling %d open order(s)...",
                len(self._open_orders),
            )
            for order_id, bet in list(self._open_orders.items()):
                try:
                    if self._try_recover_fill(bet, order_id):
                        del self._open_orders[order_id]
                        continue
                    self._clob.cancel_order(order_id)
                    bet.order_status  = "cancelled"
                    bet.cancelled_at  = _now_iso()
                    bet.cancel_reason = "shutdown"
                    del self._open_orders[order_id]
                    self._release_line_after_unfilled_order(bet, reason="shutdown")
                    self._append_to_live_ledger(bet)
                except Exception as exc:
                    LOGGER.warning(
                        "Shutdown cancel failed for [%s]: %s", bet.bet_id, exc,
                    )
        # Final orphan-fill sweep against the public Polymarket data-api so any
        # fill the SDK's maker-address-only get_order/get_trades missed gets
        # patched into the bet record before we settle and write reports.
        # See live_reconciliation.py for the 2026-05-10 incident this catches.
        self._reconcile_orphan_fills()
        self._settle_finished_games()
        self._save_session(force=True)
        self._scrape_active_date_final_games()
        self._write_daily_human_review_report()
        self._write_model_maturity_report()
        LOGGER.info("Graceful shutdown complete.")

    def _reconcile_orphan_fills(self) -> Dict[str, int]:
        """Wrapper kept on the engine for testability + subclass overrides."""
        try:
            return _reconcile_orphan_fills_impl(self)
        except Exception as exc:
            LOGGER.warning("Orphan-fill reconciliation crashed (non-fatal): %s", exc)
            return {"checked": 0, "orphans": 0, "errors": 1}

    def _scrape_active_date_final_games(self) -> None:
        """Best-effort final active-date scrape before shutdown reports.

        Side-neutral and UNDER paper labels depend on completed game JSONs in
        data/games. Startup intentionally avoids scraping in-progress active
        games; shutdown is the right place to refresh the same date after the
        live run has ended.
        """
        if bool(getattr(self, "_active_date_final_scrape_done", False)):
            return
        self._active_date_final_scrape_done = True
        script = PROJECT_DIR / "scripts" / "scraping" / "scrape_mlb_history.py"
        command = [
            sys.executable,
            str(script),
            "--start-date",
            self.date_str,
            "--end-date",
            self.date_str,
            "--game-types",
            "R",
            "--overwrite",
        ]
        try:
            result = subprocess.run(
                command,
                cwd=str(PROJECT_DIR),
                capture_output=True,
                text=True,
                timeout=300,
            )
            if result.returncode == 0:
                tail = (result.stdout or result.stderr or "").strip().splitlines()[-3:]
                LOGGER.info(
                    "Active-date final scrape complete for %s.%s",
                    self.date_str,
                    f" Tail: {' | '.join(tail)}" if tail else "",
                )
            else:
                output = (result.stderr or result.stdout or "").strip()
                LOGGER.warning(
                    "Active-date final scrape failed for %s rc=%s: %s",
                    self.date_str,
                    result.returncode,
                    output[-1000:],
                )
        except Exception as exc:
            LOGGER.warning(
                "Active-date final scrape failed for %s: %s",
                self.date_str,
                exc,
            )

    def _write_daily_human_review_report(self) -> None:
        """Write compact JSON/Markdown review for the active session."""
        if bool(getattr(self, "_daily_human_review_written", False)):
            return
        try:
            report = _build_daily_human_review_report(session_date=self.date_str)
            json_path, md_path = _write_daily_human_review_report(
                report,
                _DAILY_HUMAN_REVIEW_OUTPUT_ROOT,
            )
            self._daily_human_review_written = True
            LOGGER.info(
                "Daily human-review report written: json=%s markdown=%s",
                json_path,
                md_path,
            )
        except FileNotFoundError as exc:
            LOGGER.warning("Daily human-review report not written: %s", exc)
        except Exception as exc:
            LOGGER.warning("Daily human-review report failed: %s", exc)

    def _write_model_maturity_report(self) -> None:
        """Write conservative model-readiness JSON/Markdown report."""
        if bool(getattr(self, "_model_maturity_report_written", False)):
            return
        try:
            self._refresh_model_maturity_inputs()
            report = _build_model_maturity_report(mode="live", max_date=self.date_str)
            json_path, md_path = _write_model_maturity_report(
                report,
                _MODEL_MATURITY_OUTPUT_ROOT,
            )
            self._model_maturity_report_written = True
            LOGGER.info(
                "Model maturity report written: json=%s markdown=%s",
                json_path,
                md_path,
            )
        except Exception as exc:
            LOGGER.warning("Model maturity report failed: %s", exc)

    def _refresh_model_maturity_inputs(self) -> None:
        """Refresh model-bearing opportunity table so shutdown reports include today."""
        try:
            _refresh_calibration_opportunity_training_table([
                "--mode",
                "live",
                "--max-date",
                self.date_str,
            ])
            LOGGER.info(
                "Model maturity input table refreshed through %s.",
                self.date_str,
            )
        except SystemExit as exc:
            LOGGER.warning(
                "Model maturity input refresh exited with %s; report may use prior table.",
                exc,
            )
        except Exception as exc:
            LOGGER.warning(
                "Model maturity input refresh failed: %s; report may use prior table.",
                exc,
            )

    # ------------------------------------------------------------------
    # Limit price + stake sizing
    # ------------------------------------------------------------------

    def _compute_limit_price(
        self,
        ask: float,
        bid: float,
        fair_value: float,
        line_val: float,
    ) -> Optional[float]:
        return _compute_limit_price_impl(self, ask, bid, fair_value, line_val)

    def _filled_notional(self, bet: LiveBetRecord) -> float:
        return _filled_notional_impl(self, bet)

    def _evaluate_correlated_line_cap(self, *, game, market) -> Optional[str]:
        """Return a skip-reason string if a correlated-line cap would block
        this placement, else None.

        Two independent rules:
          1. **Count cap**: at most ``max_correlated_over_lines_per_game``
             over-side bets on the same game (default 2). Counts filled +
             open/pending bets so a cancellation sequence doesn't hide
             accumulated exposure.
          2. **Spacing cap**: every new over-side line must be at least
             ``min_correlated_line_gap`` runs away from every other
             already-placed over line on the game (default 1.5 -- blocks
             O7.5+O8.5 but allows O7.5+O9.5).

        Either rule can be disabled by setting its threshold to 0.

        Returns ``"correlated_line_count_cap"``,
        ``"correlated_line_gap_cap"``, or ``None``.
        """
        max_lines_per_game = int(getattr(
            self.live_args,
            "max_correlated_over_lines_per_game",
            DEFAULT_MAX_CORRELATED_OVER_LINES_PER_GAME,
        ))
        min_line_gap = float(getattr(
            self.live_args,
            "min_correlated_line_gap",
            DEFAULT_MIN_CORRELATED_LINE_GAP,
        ))
        if max_lines_per_game <= 0 and min_line_gap <= 0.0:
            return None

        try:
            this_line_value: Optional[float] = float(market.line)
        except (TypeError, ValueError):
            this_line_value = None

        same_game_over_bets = [
            b for b in self._bets
            if b.game_pk == game.game_pk
            and str(getattr(b, "side", "over")).lower() == "over"
            and (
                getattr(b, "order_status", "") == "filled"
                or _is_exposure_counted_status(getattr(b, "order_status", ""))
            )
        ]
        if max_lines_per_game > 0 and len(same_game_over_bets) >= max_lines_per_game:
            LOGGER.info(
                "Correlated-line count cap (%d) hit for %s@%s -- "
                "skipping line=%s (existing lines: %s)",
                max_lines_per_game, game.away_abbrev, game.home_abbrev,
                market.line,
                [getattr(b, "line", "?") for b in same_game_over_bets],
            )
            return "correlated_line_count_cap"
        if this_line_value is not None and min_line_gap > 0.0:
            for prior_bet in same_game_over_bets:
                try:
                    prior_line = float(getattr(prior_bet, "line", ""))
                except (TypeError, ValueError):
                    continue
                if abs(this_line_value - prior_line) < min_line_gap:
                    LOGGER.info(
                        "Correlated-line spacing cap hit for %s@%s "
                        "(line=%s within %.1f of existing line=%s) -- skipping",
                        game.away_abbrev, game.home_abbrev, market.line,
                        min_line_gap, prior_bet.line,
                    )
                    return "correlated_line_gap_cap"
        return None

    def _release_line_after_unfilled_order(self, bet: LiveBetRecord, *, reason: str) -> None:
        return _release_line_after_unfilled_order_impl(self, bet, reason=reason)

    def _kelly_components(
        self, fair_value: float, limit_price: float
    ) -> Tuple[float, float, float]:
        return _kelly_components_impl(self, fair_value, limit_price)

    def _compute_stake(self, fair_value: float, limit_price: float) -> float:
        return _compute_stake_impl(self, fair_value, limit_price)

    # ------------------------------------------------------------------
    # Override: _place_bet -- core difference vs paper trading
    # ------------------------------------------------------------------

    def _place_bet(
        self,
        game,
        market,
        best_ask: float,
        fair_value: float,
        base_fair_value: float,
        stage2_run_env_delta: float,
        team_offense_delta: float,
        edge: float,
        inferred_runs: int,
        inning: int,
        inning_state: str,
        outs: int,
        away_score_before: int,
        home_score_before: int,
        batting_is_away: bool,
        runners_on: int,
        decision_bid: Optional[float] = None,
        ltp: Optional[float] = None,
        state_value_diagnostics: Optional[Dict[str, object]] = None,
    ) -> Optional[BetRecord]:
        return _place_bet_impl(
            self,
            game=game,
            market=market,
            best_ask=best_ask,
            fair_value=fair_value,
            base_fair_value=base_fair_value,
            stage2_run_env_delta=stage2_run_env_delta,
            team_offense_delta=team_offense_delta,
            edge=edge,
            inferred_runs=inferred_runs,
            inning=inning,
            inning_state=inning_state,
            outs=outs,
            away_score_before=away_score_before,
            home_score_before=home_score_before,
            batting_is_away=batting_is_away,
            runners_on=runners_on,
            decision_bid=decision_bid,
            ltp=ltp,
            state_value_diagnostics=state_value_diagnostics,
        )


    # ------------------------------------------------------------------
    # Order monitoring
    # ------------------------------------------------------------------

    def _check_open_orders(self) -> None:
        return _check_open_orders_impl(self)

    def _try_recover_fill(self, bet: LiveBetRecord, order_id: str) -> bool:
        return _try_recover_fill_impl(self, bet, order_id)

    def _cancel_stale_orders(self) -> None:
        return _cancel_stale_orders_impl(self)

    def _check_ask_reversal(self) -> None:
        return _check_ask_reversal_impl(self)

    def _recompute_fv(self, bet: LiveBetRecord) -> Optional[float]:
        return _recompute_fv_impl(self, bet)

    def _check_fv_decay(self) -> None:
        return _check_fv_decay_impl(self)

    def _cancel_orders_for_game(self, game_pk: int) -> None:
        return _cancel_orders_for_game_impl(self, game_pk)

    # ------------------------------------------------------------------
    # Lifecycle overrides
    # ------------------------------------------------------------------

    def _is_bet_executable(self, bet: BetRecord) -> bool:
        """Live override: only a filled order represents an executed position."""
        return getattr(bet, "order_status", "") == "filled"

    def _settle_finished_games(self) -> None:
        """Override: cancel open orders before settling, then call parent.

        Also writes a SETTLED event to the live ledger for each settled bet.
        """
        # Cancel open orders for any finalized game before parent settlement
        for game_pk, game in list(self.games.items()):
            if game.is_final():
                self._cancel_orders_for_game(game_pk)

        # Parent settlement (computes P&L and calls _append_to_ledger).
        # Capture settled bet IDs before parent settlement so we only emit
        # one SETTLED log/ledger event per newly settled bet.
        settled_before = {
            str(getattr(b, "bet_id", ""))
            for b in self._bets
            if bool(getattr(b, "settled", False))
        }
        super()._settle_finished_games()
        newly_settled = [
            b
            for b in self._bets
            if bool(getattr(b, "settled", False))
            and str(getattr(b, "bet_id", "")) not in settled_before
        ]

        # Write SETTLED events to the live ledger
        for bet in newly_settled:
            result_str = "WIN" if bet.won else ("LOSS" if bet.won is False else "PUSH")
            ask_drop_str = (
                f"  ask_drop_5s={bet.ask_drop_5s:+.3f}"
                if getattr(bet, "ask_drop_5s", None) is not None else ""
            )
            LOGGER.info(
                "SETTLED [%s] %s@%s O/U %s %s | status=%s  %s  profit=%+.2f | "
                "final_total=%.0f (needed %.1f) | "
                "fv=%.3f  limit=%.3f  edge=%.3f  S2=%+.3f  S3=%+.3f%s",
                bet.bet_id,
                bet.away_abbrev, bet.home_abbrev, bet.line, result_str,
                getattr(bet, "order_status", "?"),
                "FILLED" if self._is_bet_executable(bet) else "MISSED",
                bet.profit or 0,
                bet.final_total or 0, float(bet.line),
                bet.fair_value, getattr(bet, "limit_price", 0),
                bet.edge, bet.stage2_run_env_delta, bet.team_offense_delta,
                ask_drop_str,
            )
            try:
                event = {
                    "_event": "settled",
                    "event_type": "settled",
                    "bet_id": bet.bet_id,
                    "order_id": getattr(bet, "order_id", None),
                    "settled_at": bet.settled_at,
                    "result": result_str,
                    "profit": bet.profit,
                    "final_total": bet.final_total,
                }
                self._write_live_ledger_row(event)
            except Exception as exc:
                LOGGER.warning("Failed to write %s event to live ledger: %s", result_str, exc)

    def _on_tick_batch(self, tick_batch: list) -> None:
        """Override monitor hook -- runs after every poll cycle.

        Calls the parent signal processing, then manages open orders on a
        throttled cadence:
          1. _check_ask_reversal()  -- poll for early cancels (not throttled)
          2. _check_open_orders()   -- poll CLOB for fills
          3. _check_fv_decay()      -- cancel if model edge has collapsed (primary path)
          4. _cancel_stale_orders() -- 3-hour safety net for abandoned orders
        """
        super()._on_tick_batch(tick_batch)

        self._poll_cycle_count += 1
        self._check_ask_reversal()

        if self._poll_cycle_count % ORDER_POLL_EVERY_N_CYCLES == 0:
            self._check_open_orders()
            self._check_fv_decay()
            self._cancel_stale_orders()
        self._flush_pending_session_save()

    def _flush_pending_session_save(self) -> None:
        if not self._session_save_pending:
            return
        min_interval = max(0.0, float(self._session_save_min_interval_secs))
        if min_interval > 0 and (_now_ts() - self._last_session_save_ts) < min_interval:
            return
        self._save_session(force=True)

    def _append_to_ledger(self, bet: BetRecord) -> None:
        return _append_to_ledger_impl(self, bet)

    def _live_bet_event_type(self, bet: LiveBetRecord) -> str:
        return _live_bet_event_type_impl(bet)

    def _append_to_live_ledger(self, bet: LiveBetRecord) -> None:
        return _append_to_live_ledger_impl(self, bet)

    def _live_ledger_event_key(self, row: Dict[str, Any]) -> Tuple[str, str, str]:
        return _live_ledger_event_key_impl(row)

    def _bootstrap_live_ledger_event_keys(self) -> None:
        return _bootstrap_live_ledger_event_keys_impl(self)

    def _write_live_ledger_row(self, row: Dict[str, Any]) -> bool:
        return _write_live_ledger_row_impl(self, row)

    def _build_shadow_order_diagnostics(self) -> Dict[str, Dict[str, object]]:
        return _build_shadow_order_diagnostics_impl(self)

    def _build_shadow_feature_diagnostics(self) -> Dict[str, Dict[str, object]]:
        return _build_shadow_feature_diagnostics_impl(self)

    def _current_state_edge_band(self, edge: Optional[float]) -> Tuple[str, str]:
        return _current_state_edge_band_impl(self, edge)

    def _build_current_state_edge_band_diagnostics(self) -> Dict[str, Dict[str, object]]:
        return _build_current_state_edge_band_diagnostics_impl(self)

    def _log_shadow_order_diagnostics(
        self, diagnostics: Dict[str, Dict[str, object]]
    ) -> None:
        return _log_shadow_order_diagnostics_impl(self, diagnostics)

    def _log_shadow_feature_diagnostics(
        self, diagnostics: Dict[str, Dict[str, object]]
    ) -> None:
        return _log_shadow_feature_diagnostics_impl(self, diagnostics)

    def _log_current_state_edge_band_diagnostics(
        self,
        diagnostics: Dict[str, Dict[str, object]],
    ) -> None:
        return _log_current_state_edge_band_diagnostics_impl(self, diagnostics)

    def _save_session(self, force: bool = False) -> None:
        return _save_session_impl(self, force=force)


# ---------------------------------------------------------------------------
# CLI entry-point. Heavy lifting (parse_live_args, logging, log rotation,
# startup-refresh wiring) lives in dedicated modules; this file owns only
# the LiveTradingEngine class and the thin main() driver below.
# ---------------------------------------------------------------------------

from live_engine_setup import (  # noqa: E402
    LOG_ROTATION_GZIP_AFTER_DAYS,
    LOG_ROTATION_RETENTION_DAYS,
    rotate_old_log_files as _rotate_old_log_files,
    run_startup_refresh as _run_startup_refresh,
    setup_logging as _setup_logging,
    suppress_noisy_library_loggers as _suppress_noisy_library_loggers,
)


def _finalize_after_run_exit(engine: LiveTradingEngine) -> None:
    """Write final non-signal-run artifacts in graceful-shutdown order."""
    engine._settle_finished_games()
    engine._save_session(force=True)
    engine._scrape_active_date_final_games()
    engine._write_daily_human_review_report()
    engine._write_model_maturity_report()


def main() -> None:
    import signal as _signal

    live_args, trade_args, monitor_args = parse_live_args()

    # Set up logging
    log_dir = PROJECT_DIR / "logs" / "real-logs"
    from zoneinfo import ZoneInfo
    tz = ZoneInfo(monitor_args.timezone)
    from datetime import datetime as _dt
    date_str = monitor_args.date or _dt.now(tz).strftime("%Y-%m-%d")
    log_path = _setup_logging(log_dir, date_str, getattr(monitor_args, "log_level", "INFO"))
    LOGGER.info("Log file: %s", log_path)

    _run_startup_refresh(
        date_str=date_str,
        live_args=live_args,
        trade_args=trade_args,
        monitor_args=monitor_args,
    )

    engine = LiveTradingEngine(
        args=monitor_args,
        trade_args=trade_args,
        live_args=live_args,
    )
    shutdown_state = {"graceful_done": False}

    def _handle_signal(signum, frame):
        LOGGER.info("Received signal %d -- initiating graceful shutdown...", signum)
        engine._shutdown_gracefully()
        shutdown_state["graceful_done"] = True
        sys.exit(0)

    _signal.signal(_signal.SIGINT, _handle_signal)
    _signal.signal(_signal.SIGTERM, _handle_signal)

    try:
        engine.run()
    except KeyboardInterrupt:
        LOGGER.info("Interrupted -- shutting down gracefully.")
        engine._shutdown_gracefully()
        shutdown_state["graceful_done"] = True
    finally:
        if not shutdown_state["graceful_done"]:
            try:
                _finalize_after_run_exit(engine)
                LOGGER.info("Final session state saved after run exit.")
            except Exception as exc:
                LOGGER.warning("Final post-run session save failed: %s", exc)


# Backward-compatible aliases
RealTradingEngine = LiveTradingEngine

if __name__ == "__main__":
    main()
