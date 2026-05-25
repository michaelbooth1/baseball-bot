#!/usr/bin/env python3
"""
signal_engine.py -- Core MLB Polymarket O/U signal detection and trading engine.

Subclasses MLBPolymarketMonitor and hooks into the tick batch to detect
scoring events via Polymarket ask price jumps, infer the new game state,
compute fair value from the Stage-1/2/3 model stack, and place bets when
edge >= threshold after passing the full multi-gate decision pipeline.

Settlement happens when the MLB API confirms a game as Final.
Results are appended to a master ledger (persistent) and a per-session file.

This engine is the foundation for live trading: LiveTradingEngine (live_engine.py)
extends SignalEngine and overrides _place_bet() for real CLOB execution.

Usage (paper / simulation mode):
    python signal_engine.py
    python signal_engine.py --date 2026-04-07
    python signal_engine.py --edge-threshold 0.10 --min-inning 4
    python signal_engine.py --stake 100 --max-spread 0.20
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import sys
import time
from collections import Counter, defaultdict
from dataclasses import asdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

PROJECT_DIR = Path(__file__).resolve().parents[2]

# Add the monitor's parent dir to path so we can import it.
sys.path.insert(0, str(PROJECT_DIR / "scripts" / "monitor"))
sys.path.insert(0, str(PROJECT_DIR / "scripts" / "analysis"))
sys.path.insert(0, str(PROJECT_DIR / "cache"))

from monitor_mlb_polymarket_ou import (  # noqa: E402
    MLBPolymarketMonitor,
    ScheduledGame,
    OUMarket,
)
from analyze_polymarket_overreactions import (  # noqa: E402
    OUCache,
    _inning_state_to_half,
)
from team_offense_model import TeamOffenseModel  # noqa: E402
from stage2_run_env_model import Stage2RunEnvModel, RunEnvContext  # noqa: E402

from models import BetRecord  # noqa: E402
from model_families import SCORE_EVENT_TRANSITION  # noqa: E402
from probability_calibration import ProbabilityCalibrator  # noqa: E402
from signal_config import *  # noqa: F401,F403
from signal_config import parse_trade_args as _parse_trade_args_impl  # noqa: E402
from signal_gates import (
    blowout_relax_pass as _blowout_relax_pass_impl,
    evaluate_shadow_relaxed as _evaluate_shadow_relaxed_impl,
    is_relax_ab_treatment as _is_relax_ab_treatment_impl,
    min_current_total_relax_pass as _min_current_total_relax_pass_impl,
    resolve_relax_mode_action as _resolve_relax_mode_action_impl,
)
from signal_pipeline import process_tick as _process_tick_impl
from capture_helpers import (  # noqa: E402
    fetch_depth_snapshot as _fetch_depth_snapshot_impl,
    start_book_capture as _start_book_capture_impl,
    start_family_b_capture as _start_family_b_capture_impl,
    start_family_c_capture as _start_family_c_capture_impl,
    start_tape_capture as _start_tape_capture_impl,
)
from session_serialization import (  # noqa: E402
    build_paper_session_payload as _build_paper_session_payload,
)
# LineState + small pure helpers live in line_state.py; re-exported here so
# `from signal_engine import LineState, _now_iso, _now_ts` keeps working for
# live_engine, paper_trader, tests, and any external callers.
from line_state import (  # noqa: E402,F401
    LineState,
    _ask_edge_boost,
    _mid,
    _now_iso,
    _now_ts,
    _runs_pace_ok,
)
from candidate_logging import (  # noqa: E402
    build_candidate_skip_dedup_key as _build_candidate_skip_dedup_key_impl,
    candidate_calibration_log_path as _candidate_calibration_log_path_impl,
    candidate_log_path as _candidate_log_path_impl,
    candidate_rollup_path as _candidate_rollup_path_impl,
    candidate_rollup_snapshot as _candidate_rollup_snapshot_impl,
    ensure_candidate_rollup_state as _ensure_candidate_rollup_state_impl,
    flush_expired_score_confirmations as _flush_expired_score_confirmations_impl,
    log_skip_debug_once as _log_skip_debug_once_impl,
    next_candidate_id as _next_candidate_id_impl,
    observe_score_confirmation_ticks as _observe_score_confirmation_ticks_impl,
    observe_candidate_rollup as _observe_candidate_rollup_impl,
    outcome_log_path as _outcome_log_path_impl,
    record_candidate_decision as _record_candidate_decision_impl,
    score_confirmation_log_path as _score_confirmation_log_path_impl,
    write_candidate_rollup as _write_candidate_rollup_impl,
    write_outcome_record as _write_outcome_record_impl,
)
from runtime_log_rollups import (  # noqa: E402
    ensure_runtime_log_rollup_state as _ensure_runtime_log_rollup_state,
    log_runtime_debug_rollups as _log_runtime_debug_rollups_impl,
)
from weather_client import (  # noqa: E402
    default_weather_cache_path as _default_weather_cache_path,
    load_weather_features_by_game as _load_weather_features_by_game,
)

LOGGER = logging.getLogger("signal_engine")

# Backward-compatible alias so existing code and session files keep working
PaperBet = BetRecord

# ---------------------------------------------------------------------------
# Constants / defaults
# ---------------------------------------------------------------------------

# Phase 2 refactor: defaults and CLI parsing moved to signal_config.py.
# Keep these names available from signal_engine for backward compatibility.


# LineState dataclass and the small pure helpers (_mid, _now_iso, _now_ts,
# _runs_pace_ok, _ask_edge_boost) live in line_state.py and are re-exported
# at the top of this file for backward compat.


# ---------------------------------------------------------------------------
# Startup-time artifact lineage helper (Active #16 v3, 2026-05-17).
# ---------------------------------------------------------------------------


def _log_artifact_lineage_summary(
    label: str,
    path,
    *,
    expected: bool = True,
) -> None:
    """Read the lineage block from an artifact at `path` and log a
    one-line summary at INFO level. Fail-open: ANY error reading the
    artifact (missing, malformed, missing lineage block) is logged at
    DEBUG and silently swallowed. Startup must never block on a
    lineage read.

    `expected=True` means the artifact is required for the engine to
    function (Stage-1/Stage-2/Calibrator); a missing-lineage warning
    surfaces at INFO. `expected=False` means the artifact is optional
    (Stage-3 v2 weights override that may not exist yet); missing
    artifact silently logged at DEBUG.
    """
    try:
        import sys as _sys
        analysis_dir = PROJECT_DIR / "scripts" / "analysis"
        if str(analysis_dir) not in _sys.path:
            _sys.path.insert(0, str(analysis_dir))
        from artifact_lineage import (
            _read_lineage_from_path,
            format_lineage_summary_line,
        )
    except ImportError as exc:
        LOGGER.debug(
            "Could not import artifact_lineage for %s: %s", label, exc,
        )
        return

    try:
        p = Path(path) if path is not None else None
        if p is None or not p.exists():
            if expected:
                LOGGER.info(
                    "Artifact lineage: %s: artifact not found at %s",
                    label, path,
                )
            else:
                LOGGER.debug(
                    "Artifact lineage: %s: optional artifact not present at %s",
                    label, path,
                )
            return
        lineage = _read_lineage_from_path(p)
        line = format_lineage_summary_line(label, lineage)
        LOGGER.info("Artifact lineage: %s", line)
    except Exception as exc:  # noqa: BLE001
        LOGGER.debug(
            "Artifact lineage logging failed for %s: %r", label, exc,
        )


# ---------------------------------------------------------------------------
# Paper trading engine
# ---------------------------------------------------------------------------

class SignalEngine(MLBPolymarketMonitor):

    def __init__(self, args: argparse.Namespace, trade_args: argparse.Namespace):
        super().__init__(args)
        self.trade_args = trade_args
        self._weather_cache_path = _default_weather_cache_path(self.date_str)
        self._weather_features_by_game_pk: Dict[int, Dict[str, object]] = {}
        self._weather_cache_loaded = False
        self._load_weather_feature_cache()
        self.cache = OUCache(trade_args.cache_path)
        LOGGER.info("Cache loaded: %d cells", len(self.cache.cells))
        # Active #16 v3 (2026-05-17): log build-time lineage for each
        # artifact loaded at boot. Operator can grep the runtime log
        # for "Artifact lineage:" and see which Stage-1/2/3/calibrator
        # version was in production for any given session. Fail-open:
        # any error reading the lineage block must not block startup.
        _log_artifact_lineage_summary("stage1_cache", trade_args.cache_path)

        # [Stage-2] Park/weather run-environment model.
        try:
            self.stage2_model: Optional[Stage2RunEnvModel] = Stage2RunEnvModel.from_path(
                trade_args.stage2_model_path
            )
            LOGGER.info(
                "Stage-2 run-env model loaded: lines=%s  max_delta=%.2f",
                self.stage2_model.lines,
                self.stage2_model.max_total_abs_delta,
            )
            _log_artifact_lineage_summary(
                "stage2_cache", trade_args.stage2_model_path,
            )
        except Exception as exc:
            LOGGER.warning("Stage-2 model not loaded (%s); park/weather adjustments disabled.", exc)
            self.stage2_model = None

        # [Stage-3] Team offense adjustment model â€” calibrated on 12,457 games.
        self.offense_model = TeamOffenseModel.load(
            game_log_path=trade_args.team_game_log_path,
            auto_rebuild=True,
        )
        LOGGER.info(
            "Team offense model loaded: mlb_avg_rpg=%.3f  mlb_avg_total=%.3f  window=%d games",
            self.offense_model.mlb_avg_rpg,
            self.offense_model.mlb_avg_total,
            self.offense_model.n_games,
        )
        # The Stage-3 v2 weights JSON (produced by promote_team_offense_v2.py)
        # is auto-loaded by TeamOffenseModel.load when present. Surface
        # its lineage independently so the operator can verify which
        # weights version was active without grepping the model state.
        _stage3_weights_path = (
            PROJECT_DIR / "cache" / "team_offense_v2_weights.json"
        )
        _log_artifact_lineage_summary(
            "stage3_v2_weights", _stage3_weights_path, expected=False,
        )

        self._prob_calibration_mode = str(
            getattr(trade_args, "prob_calibration_mode", DEFAULT_PROB_CALIBRATION_MODE) or "off"
        ).strip().lower()
        self._prob_calibration_path = Path(
            getattr(trade_args, "prob_calibration_path", DEFAULT_PROB_CALIBRATION_PATH)
        )
        self._prob_calibration_enforce_min_raw = float(
            getattr(
                trade_args,
                "prob_calibration_enforce_min_raw",
                DEFAULT_PROB_CALIBRATION_ENFORCE_MIN_RAW,
            )
        )
        self._prob_calibrator: Optional[ProbabilityCalibrator] = None
        self._prob_calibration_stats: Dict[str, int] = {
            "scored": 0,
            "applied": 0,
            "shadow_scored": 0,
            "below_min_raw_kept_raw": 0,
            "disabled_or_missing": 0,
            "family_missing": 0,
            "family_missing_fail_closed": 0,
        }
        self._prob_calibration_missing_family_warned: set = set()
        if self._prob_calibration_mode not in {"off", "shadow", "enforce"}:
            LOGGER.warning(
                "Unknown prob calibration mode '%s'; forcing off.",
                self._prob_calibration_mode,
            )
            self._prob_calibration_mode = "off"
        if self._prob_calibration_mode != "off":
            try:
                self._prob_calibrator = ProbabilityCalibrator.from_path(self._prob_calibration_path)
                LOGGER.info(
                    "Probability calibrator loaded: mode=%s method=%s path=%s",
                    self._prob_calibration_mode,
                    self._prob_calibrator.method,
                    self._prob_calibration_path,
                )
                _log_artifact_lineage_summary(
                    "calibrator", self._prob_calibration_path,
                )
                # Identity = no-op. Mode says shadow/enforce, but the artifact
                # short-circuits to the raw probability, so calibrated_fv_*
                # rows in the candidate ledger and downstream maturity reports
                # silently match raw_fv_*. Surface this loudly so a stale or
                # degenerate-validation artifact can't go unnoticed.
                if str(self._prob_calibrator.method).lower() == "identity":
                    LOGGER.warning(
                        "Probability calibrator artifact at %s selected method='identity' "
                        "(mode=%s). Calibrated probabilities will equal raw probabilities. "
                        "Rebuild via scripts/analysis/calibrate_signal_probabilities.py "
                        "with sufficient settled rows and a non-degenerate validation split.",
                        self._prob_calibration_path,
                        self._prob_calibration_mode,
                    )
            except Exception as exc:
                LOGGER.warning(
                    "Probability calibrator unavailable (%s): %s. Calibration disabled.",
                    self._prob_calibration_path,
                    exc,
                )
                self._prob_calibration_mode = "off"
                self._prob_calibrator = None

        # Active #8 prep (2026-05-17). Stage-1 shadow empirical
        # override. Default `off`; operator opts in via
        # `--stage1-shadow-empirical-override shadow` on
        # live_engine_cli.py (live_engine.py bridges live_args ->
        # trade_args). When `shadow`, the post-FV phase computes
        # `fair_value_alt_empirical` alongside production fair_value
        # whenever the cell's empirical estimate is present, and
        # logs both on the candidate row. NO effect on decisions.
        self._stage1_shadow_empirical_mode = str(
            getattr(trade_args, "stage1_shadow_empirical_mode", "off")
            or "off"
        ).strip().lower()
        if self._stage1_shadow_empirical_mode not in {"off", "shadow"}:
            LOGGER.warning(
                "Unknown stage1 shadow empirical mode '%s'; forcing off.",
                self._stage1_shadow_empirical_mode,
            )
            self._stage1_shadow_empirical_mode = "off"
        if self._stage1_shadow_empirical_mode == "shadow":
            LOGGER.info(
                "Stage-1 shadow empirical override mode: shadow "
                "(logs fair_value_alt_empirical per candidate; no "
                "decision change)"
            )

        # Active #17 (2026-05-21): Scoped Alt-A enforce mode.
        # Default `shadow` -- compute scope decision + log it but
        # don't swap FV. Operator audits per-candidate rule matches
        # before flipping to `enforce`.
        self._stage1_alt_a_scope_mode = str(
            getattr(trade_args, "stage1_alt_a_scope_mode",
                    DEFAULT_STAGE1_ALT_A_SCOPE_MODE)
            or DEFAULT_STAGE1_ALT_A_SCOPE_MODE
        ).strip().lower()
        if self._stage1_alt_a_scope_mode not in set(STAGE1_ALT_A_SCOPE_MODES):
            LOGGER.warning(
                "Unknown stage1 Alt-A scope mode '%s'; forcing %s.",
                self._stage1_alt_a_scope_mode, DEFAULT_STAGE1_ALT_A_SCOPE_MODE,
            )
            self._stage1_alt_a_scope_mode = DEFAULT_STAGE1_ALT_A_SCOPE_MODE
        if self._stage1_alt_a_scope_mode != "off":
            LOGGER.info(
                "Stage-1 Alt-A scope mode: %s (%d rules in policy; "
                "default action=%s)",
                self._stage1_alt_a_scope_mode,
                len(STAGE1_ALT_A_SCOPE_RULES),
                STAGE1_ALT_A_SCOPE_DEFAULT_ACTION,
            )

        # Phase A5 (2026-05-19). UNDER candidate emission. Default
        # `off`; operator opts in via `--under-emission-mode shadow`
        # on live_engine_cli.py. When `shadow`, alongside every OVER
        # candidate that reaches the FV phase, the engine emits a
        # sibling UNDER candidate row (decision_reason = `shadow_under`
        # when UNDER gates pass). NO UNDER bets are placed in either
        # mode -- pure observability for the bidirectional pivot.
        self._under_emission_mode = str(
            getattr(trade_args, "under_emission_mode", "off") or "off"
        ).strip().lower()
        if self._under_emission_mode not in {"off", "shadow"}:
            LOGGER.warning(
                "Unknown UNDER emission mode '%s'; forcing off.",
                self._under_emission_mode,
            )
            self._under_emission_mode = "off"
        self._under_prob_calibrator: Optional[ProbabilityCalibrator] = None
        if self._under_emission_mode == "shadow":
            under_cal_path = Path(
                getattr(trade_args, "prob_calibration_under_path",
                        DEFAULT_PROB_CALIBRATION_UNDER_PATH)
            )
            try:
                self._under_prob_calibrator = ProbabilityCalibrator.from_path(
                    under_cal_path,
                )
                LOGGER.warning(
                    "UNDER emission mode: shadow -- UNDER CALIBRATOR IS "
                    "UNRELIABLE. Loaded from %s (method=%s), but the "
                    "2026-05-22 debut day showed FV severely off: "
                    "DET@BAL O7.5 inn4 model said P(under)=0.73, game "
                    "ended at 11 runs. All 17 emitted UNDER candidates "
                    "would have lost full stake at $10/bet = $170. "
                    "Treat shadow_under counterfactual P&L with extreme "
                    "skepticism; do NOT promote to live until calibrator "
                    "is refit on a much larger UNDER sample. Candidate "
                    "rows stamped shadow_under_calibration_status="
                    "unreliable_pre_refit so downstream cohort blocks "
                    "can filter.",
                    under_cal_path,
                    self._under_prob_calibrator.method,
                )
                _log_artifact_lineage_summary(
                    "calibrator_under", under_cal_path,
                )
            except Exception as exc:  # noqa: BLE001
                LOGGER.warning(
                    "UNDER calibrator unavailable at %s (%s); UNDER "
                    "emission continues with identity calibration "
                    "(under_fv = 1 - over_fv_raw uncalibrated).",
                    under_cal_path, exc,
                )
                self._under_prob_calibrator = None

        # Phase C C1+C2+C3+C4 shadow (2026-05-17). Two-sided quote
        # engine. Default `off`; operator opts in via
        # `--quote-engine-mode shadow` from live_engine_cli.py. When
        # on, the per-tick hook in signal_pipeline computes a
        # QuoteDecision + appends to a per-date shadow ledger.
        # No order is ever placed in shadow mode.
        self._quote_engine_mode = str(
            getattr(trade_args, "quote_engine_mode", "off") or "off"
        ).strip().lower()
        if self._quote_engine_mode not in {"off", "shadow"}:
            LOGGER.warning(
                "Unknown quote engine mode '%s'; forcing off.",
                self._quote_engine_mode,
            )
            self._quote_engine_mode = "off"
        # Lazy-imported to avoid loading the quote engine module
        # path on engines that don't use it. Pure-function module;
        # safe to import unconditionally too, but lazy is fine.
        self._quote_engine_config = None
        self._inventory_snapshot_cache = None
        if self._quote_engine_mode == "shadow":
            from live_quote_engine import QuoteEngineConfig
            self._quote_engine_config = QuoteEngineConfig()
            LOGGER.info(
                "Quote engine mode: shadow (writes to %s/quote_engine_shadow/)",
                trade_args.paper_root,
            )

        # State per (game_pk, line)
        self._line_states: Dict[Tuple[int, str], LineState] = {}

        # [TR2 Change 5] Per-game last bet timestamp for same-event dedup
        self._last_bet_ts: Dict[int, float] = {}
        self._last_bet_edge: Dict[int, float] = {}          # keyed by game_pk (same-event Gate 8)

        # [TR4 Change 2] Per-(game_pk, line) last bet inning for cross-inning dedup
        # Tracks per line so 7.5 and 8.5 dedup windows are independent
        self._last_bet_inning: Dict[Tuple[int, str], int] = {}
        # [TR5 P2 fix] Per-(game_pk, line) last bet edge â€” prevents game-level edge
        # from one line incorrectly gating or unlocking another line's dedup window
        self._last_bet_edge_by_line: Dict[Tuple[int, str], float] = {}

        # All bets placed this session
        self._bets: List[BetRecord] = []
        self._bet_counter = 0
        self._candidate_seq = 0
        self._candidate_mode = "paper"
        self._candidate_rows_written = 0
        self._candidate_rows_dedup_suppressed = 0
        self._candidate_raw_sample_suppressed = 0
        self._candidate_rows_write_errors = 0
        self._candidate_null_fields_omitted = 0
        self._candidate_compacted_fields_omitted = 0
        self._candidate_calibration_rows_written = 0
        self._candidate_calibration_write_errors = 0
        self._score_confirmation_pending: Dict[str, Dict[str, object]] = {}
        self._score_confirmation_rows_written = 0
        self._score_confirmation_write_errors = 0
        self._candidate_skip_dedup_seen = set()
        self._candidate_raw_sample_seen: Counter[tuple, int] = Counter()
        self._candidate_rollup_by_write_status: Counter[str] = Counter()
        self._candidate_rollup_by_decision: Counter[str] = Counter()
        self._candidate_rollup_by_reason: Counter[str] = Counter()
        self._candidate_rollup_by_strategy: Counter[str] = Counter()
        self._candidate_rollup_by_game_line_reason: Counter[str] = Counter()
        self._skip_debug_logs_emitted = 0
        self._skip_debug_logs_dedup_suppressed = 0
        self._skip_debug_seen = set()
        _ensure_runtime_log_rollup_state(self)
        # Dedup for skip-with-features capture — same key shape as
        # _skip_debug_seen so we get one feature snapshot per unique
        # game state per gate (not 100 identical snapshots while the
        # bot polls the same idle market every 2.5s).
        self._skip_features_seen: set = set()
        self._skip_features_captured = 0
        self._skip_features_dedup_suppressed = 0
        self._last_tick_buffer_health_log_ts = 0.0
        self._shadow_relaxed_evaluated = 0
        self._shadow_relaxed_would_pass = 0
        self._shadow_relaxed_blocked = 0
        self._shadow_relaxed_eval_by_reason: Dict[str, int] = defaultdict(int)
        self._shadow_relaxed_would_pass_by_reason: Dict[str, int] = defaultdict(int)
        # Gate 1 / Gate 3 also keep session-level shadow counters so volume
        # remains visible even when candidate rows are deduped.
        self._shadow_gate1_blocked: int = 0         # blocked by inning minimum
        self._shadow_gate1_would_pass: int = 0      # would pass if min_inning offset relaxed
        self._shadow_gate3_blocked: int = 0         # blocked by min entry ask
        self._shadow_gate3_would_pass: int = 0      # would pass if ask floor reduced by 0.05
        # Tracks which (game_pk, line) pairs have had outcome records written to the
        # outcomes JSONL.  Prevents duplicate writes across multiple settle cycles.
        self._outcome_games_written: set = set()

        # Output paths
        paper_root = trade_args.paper_root
        paper_root.mkdir(parents=True, exist_ok=True)
        (paper_root / "sessions").mkdir(exist_ok=True)

        self._ledger_path = paper_root / "master_ledger.jsonl"
        self._session_path = paper_root / "sessions" / f"{self.date_str}_session.json"

        LOGGER.info("Ledger: %s", self._ledger_path)
        LOGGER.info("Session: %s", self._session_path)
        s2_status = "loaded" if self.stage2_model is not None else "DISABLED"
        LOGGER.info(
            "TR10 params: min_ask=%.2f(std)/%.2f(hi)  min_edge=%.2f(std)/%.2f(hi)  "
            "runs_needed_max=%.1f  close_game_rn=%.1f(lead<2)  "
            "inn5_rn_max=%.1f  inn6_rn_max=%.1f  "
            "min_total=%d  min_total_relax=%s(i=%d,t=%d,ask>=%.2f,lead<=%d,rn<=%.2f)  "
            "blowout_relax=%s(inn<=%d,ask>=%.2f,rn<=%.2f)  "
            "ask_edge_ramp=%s(start=%.2f,end=%.2f,max=%.3f)  "
            "prob_calibration=%s(min_raw=%.2f)  "
            "min_inning_high_line=%d(>=%.1f)  confirm=%d  "
            "dedup=%.0fs  inning_dedup=%d(per-line)  "
            "capture=%.0fs  stage2=%s  stage3=loaded",
            trade_args.min_entry_ask,
            trade_args.min_entry_ask_high_line,
            trade_args.edge_threshold,
            trade_args.edge_threshold_high_line,
            trade_args.runs_needed_max,
            trade_args.min_close_game_rn,
            trade_args.inn5_rn_max,
            trade_args.inn6_rn_max,
            trade_args.min_current_total,
            str(getattr(trade_args, "gate_min_current_total_relax_mode", DEFAULT_GATE_MIN_CURRENT_TOTAL_RELAX_MODE)),
            int(getattr(trade_args, "min_current_total_relax_inning", DEFAULT_MIN_CURRENT_TOTAL_RELAX_INNING)),
            int(getattr(trade_args, "min_current_total_relax_floor", DEFAULT_MIN_CURRENT_TOTAL_RELAX_FLOOR)),
            float(getattr(trade_args, "min_current_total_relax_ask_min", DEFAULT_MIN_CURRENT_TOTAL_RELAX_ASK_MIN)),
            int(getattr(trade_args, "min_current_total_relax_max_lead", DEFAULT_MIN_CURRENT_TOTAL_RELAX_MAX_LEAD)),
            float(getattr(trade_args, "min_current_total_relax_max_runs_needed", DEFAULT_MIN_CURRENT_TOTAL_RELAX_MAX_RUNS_NEEDED)),
            str(getattr(trade_args, "gate_blowout_relax_mode", DEFAULT_GATE_BLOWOUT_RELAX_MODE)),
            int(getattr(trade_args, "blowout_relax_max_inning", DEFAULT_BLOWOUT_RELAX_MAX_INNING)),
            float(getattr(trade_args, "blowout_relax_min_ask", DEFAULT_BLOWOUT_RELAX_MIN_ASK)),
            float(getattr(trade_args, "blowout_relax_max_runs_needed", DEFAULT_BLOWOUT_RELAX_MAX_RUNS_NEEDED)),
            "on" if bool(getattr(trade_args, "ask_edge_ramp_enabled", DEFAULT_ASK_EDGE_RAMP_ENABLED)) else "off",
            float(getattr(trade_args, "ask_edge_ramp_start", DEFAULT_ASK_EDGE_RAMP_START)),
            float(getattr(trade_args, "ask_edge_ramp_end", DEFAULT_ASK_EDGE_RAMP_END)),
            float(getattr(trade_args, "ask_edge_ramp_max_boost", DEFAULT_ASK_EDGE_RAMP_MAX_BOOST)),
            self._prob_calibration_mode,
            self._prob_calibration_enforce_min_raw,
            trade_args.min_inning_high_line,
            trade_args.high_line_cutoff,
            trade_args.confirmation_ticks,
            trade_args.event_dedup_secs,
            trade_args.inning_dedup_gap,
            trade_args.capture_duration,
            s2_status,
        )

    def _is_relax_ab_treatment(self, *, gate_name: str, game_pk: int, line: str) -> bool:
        return _is_relax_ab_treatment_impl(
            trade_args=self.trade_args,
            date_str=self.date_str,
            gate_name=gate_name,
            game_pk=game_pk,
            line=line,
        )

    def _resolve_relax_mode_action(
        self,
        *,
        mode: str,
        gate_name: str,
        game_pk: int,
        line: str,
        relax_pass: bool,
    ) -> Tuple[bool, str]:
        return _resolve_relax_mode_action_impl(
            trade_args=self.trade_args,
            date_str=self.date_str,
            mode=mode,
            gate_name=gate_name,
            game_pk=game_pk,
            line=line,
            relax_pass=relax_pass,
        )

    def _min_current_total_relax_pass(
        self,
        *,
        inning: int,
        current_total: int,
        ask: float,
        lead_abs: int,
        runs_needed: Optional[float],
    ) -> bool:
        return _min_current_total_relax_pass_impl(
            trade_args=self.trade_args,
            inning=inning,
            current_total=current_total,
            ask=ask,
            lead_abs=lead_abs,
            runs_needed=runs_needed,
        )

    def _blowout_relax_pass(
        self,
        *,
        inning: int,
        ask: float,
        runs_needed: Optional[float],
    ) -> bool:
        return _blowout_relax_pass_impl(
            trade_args=self.trade_args,
            inning=inning,
            ask=ask,
            runs_needed=runs_needed,
        )

    def _evaluate_correlated_line_cap(self, *, game, market) -> Optional[str]:
        """Return a skip-reason string if a correlated-line cap would
        block this placement, else None. Paper-mode version; lives in
        SignalEngine so both paper and live inherit it. Live overrides
        with a status-aware filter (`live_engine.py`) for richer
        accounting of pending/filled order states.

        Two independent rules (matching the live override's contract):
          - **Count cap** (default 2): at most N over-side bets per game
          - **Spacing cap** (default 1.5): every new over line must be
            at least G runs away from already-placed over lines

        Set either flag to 0 to disable that rule.
        """
        max_lines_per_game = int(getattr(
            self.trade_args,
            "max_correlated_over_lines_per_game",
            DEFAULT_MAX_CORRELATED_OVER_LINES_PER_GAME,
        ))
        min_line_gap = float(getattr(
            self.trade_args,
            "min_correlated_line_gap",
            DEFAULT_MIN_CORRELATED_LINE_GAP,
        ))
        if max_lines_per_game <= 0 and min_line_gap <= 0.0:
            return None

        try:
            this_line_value: Optional[float] = float(market.line)
        except (TypeError, ValueError):
            this_line_value = None

        # Paper version: every bet on the same game's over side counts
        # as exposure. No order_status filter -- paper bets don't carry
        # one and any settled bet is effectively "placed."
        same_game_over_bets = [
            b for b in self._bets
            if getattr(b, "game_pk", None) == game.game_pk
            and str(getattr(b, "side", "over")).lower() == "over"
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

    def _calibrate_fair_value(
        self,
        *,
        raw_prob: float,
        line: Optional[str] = None,
        inning: Optional[int] = None,
        decision_ask: Optional[float] = None,
        model_family: str = SCORE_EVENT_TRANSITION,
    ) -> Tuple[float, Dict[str, object]]:
        raw = min(max(float(raw_prob), 1e-8), 1.0 - 1e-8)
        calibrator_method = "identity"
        if self._prob_calibrator:
            method_for_family = getattr(self._prob_calibrator, "method_for_family", None)
            if callable(method_for_family):
                calibrator_method = str(method_for_family(model_family))
            else:
                calibrator_method = str(getattr(self._prob_calibrator, "method", "identity"))
        diag: Dict[str, object] = {
            "mode": self._prob_calibration_mode,
            "method": calibrator_method,
            "model_family": model_family,
            "family_missing": False,
            "family_fallback_used": False,
            "fail_closed": False,
            "raw_prob": raw,
            "calibrated_prob": raw,
            "delta": 0.0,
            "applied": False,
            "line": line,
            "inning": inning,
            "decision_ask": decision_ask,
        }
        if self._prob_calibration_mode == "off" or self._prob_calibrator is None:
            self._prob_calibration_stats["disabled_or_missing"] += 1
            return raw, diag

        has_family = getattr(self._prob_calibrator, "has_family", None)
        family_missing = (
            callable(has_family)
            and not bool(has_family(model_family))
        )
        if family_missing:
            self._prob_calibration_stats["family_missing"] = (
                self._prob_calibration_stats.get("family_missing", 0) + 1
            )
            diag["family_missing"] = True
            warn_key = str(model_family or "")
            warned = getattr(self, "_prob_calibration_missing_family_warned", set())
            if warn_key not in warned:
                warned.add(warn_key)
                self._prob_calibration_missing_family_warned = warned
                LOGGER.warning(
                    "Probability calibration family '%s' missing from artifact %s "
                    "(mode=%s). Shadow leaves probability uncalibrated; enforce fails closed.",
                    model_family,
                    getattr(self, "_prob_calibration_path", ""),
                    self._prob_calibration_mode,
                )
            if self._prob_calibration_mode == "enforce":
                fail_closed_prob = 1e-8
                self._prob_calibration_stats["scored"] += 1
                self._prob_calibration_stats["family_missing_fail_closed"] = (
                    self._prob_calibration_stats.get("family_missing_fail_closed", 0) + 1
                )
                diag.update(
                    {
                        "method": "missing_family_fail_closed",
                        "calibrated_prob": fail_closed_prob,
                        "delta": fail_closed_prob - raw,
                        "applied": True,
                        "fail_closed": True,
                    }
                )
                return fail_closed_prob, diag
            self._prob_calibration_stats["scored"] += 1
            self._prob_calibration_stats["shadow_scored"] += 1
            diag.update(
                {
                    "method": "missing_family_identity",
                    "calibrated_prob": raw,
                    "delta": 0.0,
                    "applied": False,
                    "family_fallback_used": False,
                }
            )
            return raw, diag

        try:
            calibrated_raw = self._prob_calibrator.calibrate(
                raw,
                model_family=model_family,
                allow_family_fallback=False,
            )
        except TypeError:
            calibrated_raw = self._prob_calibrator.calibrate(raw)
        calibrated = min(max(calibrated_raw, 1e-8), 1.0 - 1e-8)
        # Band-gated enforce (2026-05-19): only overwrite raw FV when
        # raw >= threshold. Below threshold the calibrator is still
        # scored (calibrated_prob + delta logged) but raw is kept --
        # shadow-like behavior for the mid-band where the calibrator
        # over-pulls. See docs/operational/fv-recalibration-2026-05-19.md.
        below_threshold = (
            self._prob_calibration_mode == "enforce"
            and raw < self._prob_calibration_enforce_min_raw
        )
        applied = (
            self._prob_calibration_mode == "enforce"
            and not below_threshold
        )
        final_prob = calibrated if applied else raw

        self._prob_calibration_stats["scored"] += 1
        if applied:
            self._prob_calibration_stats["applied"] += 1
        elif below_threshold:
            self._prob_calibration_stats["below_min_raw_kept_raw"] += 1
        else:
            self._prob_calibration_stats["shadow_scored"] += 1

        diag.update(
            {
                "calibrated_prob": calibrated,
                "delta": calibrated - raw,
                "applied": applied,
                "below_min_raw_kept_raw": below_threshold,
                "enforce_min_raw_threshold": self._prob_calibration_enforce_min_raw,
                "family_fallback_used": bool(family_missing),
            }
        )
        return final_prob, diag

    # ------------------------------------------------------------------
    # Weather-v2 live feature cache.
    # Startup refresh writes cache/weather/game_weather_<date>.json before
    # engine construction. These fields are the only live weather source fed
    # into Stage-2 run-env FV adjustment; missing rows degrade to unknown
    # weather buckets rather than legacy schedule text.
    # ------------------------------------------------------------------

    def _load_weather_feature_cache(self) -> None:
        try:
            if not self._weather_cache_path.exists():
                LOGGER.info(
                    "Weather v2 cache not found for %s at %s; Stage-2 weather buckets will be unknown.",
                    self.date_str,
                    self._weather_cache_path,
                )
                return
            self._weather_features_by_game_pk = _load_weather_features_by_game(self._weather_cache_path)
            self._weather_cache_loaded = True
            LOGGER.info(
                "Weather v2 cache loaded: games=%d path=%s",
                len(self._weather_features_by_game_pk),
                self._weather_cache_path,
            )
        except Exception as exc:
            self._weather_features_by_game_pk = {}
            self._weather_cache_loaded = False
            LOGGER.warning("Weather v2 cache failed to load (%s); Stage-2 weather buckets will be unknown.", exc)

    def _weather_fields_for_game(self, game_pk: int) -> Dict[str, object]:
        try:
            key = int(game_pk)
        except Exception:
            key = -1
        by_game = getattr(self, "_weather_features_by_game_pk", {}) or {}
        row = by_game.get(key)
        if row:
            return dict(row)
        if getattr(self, "_weather_cache_loaded", False):
            return {"weather_cache_available": False}
        return {}

    # ------------------------------------------------------------------
    # Candidate logging surfaces (raw JSONL, rollup counters, skip dedup).
    # Implementations live in candidate_logging.py; methods below are thin
    # delegators preserved on the class so test stubs (e.g.
    # `engine._candidate_log_path = lambda: path`) and any future subclass
    # overrides continue to work.
    # ------------------------------------------------------------------

    def _next_candidate_id(self, game_pk: int, line: str) -> str:
        return _next_candidate_id_impl(self, game_pk, line)

    def _candidate_log_path(self) -> Path:
        return _candidate_log_path_impl(self)

    def _candidate_calibration_log_path(self) -> Path:
        return _candidate_calibration_log_path_impl(self)

    def _score_confirmation_log_path(self) -> Path:
        return _score_confirmation_log_path_impl(self)

    def _candidate_rollup_path(self) -> Path:
        return _candidate_rollup_path_impl(self)

    def _outcome_log_path(self) -> Path:
        return _outcome_log_path_impl(self)

    def _write_outcome_record(
        self, *, game_pk: int, line: str, final_away: int, final_home: int
    ) -> None:
        return _write_outcome_record_impl(
            self,
            game_pk=game_pk,
            line=line,
            final_away=final_away,
            final_home=final_home,
        )

    def _ensure_candidate_rollup_state(self) -> None:
        return _ensure_candidate_rollup_state_impl(self)

    def _observe_candidate_rollup(self, row: Dict[str, object], *, write_status: str) -> None:
        return _observe_candidate_rollup_impl(self, row, write_status=write_status)

    def _candidate_rollup_snapshot(self, *, top_n: int = 50) -> Dict[str, object]:
        return _candidate_rollup_snapshot_impl(self, top_n=top_n)

    def _write_candidate_rollup(self) -> None:
        return _write_candidate_rollup_impl(self)

    def _observe_score_confirmation_ticks(self, tick_batch: list) -> None:
        return _observe_score_confirmation_ticks_impl(self, tick_batch)

    def _flush_expired_score_confirmations(self) -> None:
        return _flush_expired_score_confirmations_impl(self)

    def _build_candidate_skip_dedup_key(self, row: Dict[str, object]) -> Optional[Tuple[str, ...]]:
        return _build_candidate_skip_dedup_key_impl(row)

    def _record_candidate_decision(self, payload: Dict[str, object]) -> None:
        return _record_candidate_decision_impl(self, payload)

    def _log_skip_debug_once(
        self,
        *,
        reason: str,
        game: ScheduledGame,
        market: OUMarket,
        inning: int,
        inning_state: str,
        outs: int,
        current_total: int,
        message: str,
        args: Tuple[object, ...],
    ) -> None:
        return _log_skip_debug_once_impl(
            self,
            reason=reason,
            game=game,
            market=market,
            inning=inning,
            inning_state=inning_state,
            outs=outs,
            current_total=current_total,
            message=message,
            args=args,
        )

    def _evaluate_shadow_relaxed(
        self,
        *,
        reason: str,
        values: Optional[Dict[str, object]] = None,
    ) -> Dict[str, object]:
        return _evaluate_shadow_relaxed_impl(
            trade_args=self.trade_args,
            reason=reason,
            values=values,
        )

    # ------------------------------------------------------------------
    # Hook called by monitor after every poll cycle
    # ------------------------------------------------------------------

    def _on_tick_batch(self, tick_batch: list) -> None:
        self._observe_score_confirmation_ticks(tick_batch)

        # Keep the latest valid over_yes tick per (game_pk, line). Previous
        # versions accumulated a list and then used items[-1], which did extra
        # allocation work during high-volume polling without changing behavior.
        by_key: Dict[Tuple[int, str], tuple] = {}
        under_by_key: Dict[Tuple[int, str], tuple] = {}
        for game, market, side, payload in tick_batch:
            book = payload.get("book", {})
            if not book.get("ok"):
                continue
            key = (game.game_pk, market.line)
            if side == "under_no":
                under_by_key[key] = (payload, book)
                continue
            if side == "over_yes":
                ask = book.get("best_ask")
                if ask is None:
                    continue
                by_key[key] = (game, market, payload, book, ask)

        for game, market, payload, book, ask in by_key.values():
            under_pair = under_by_key.get((game.game_pk, market.line))
            if under_pair is not None:
                _under_payload, under_book = under_pair
                # Attach complement top-of-book fields to the Over book passed
                # into the trading pipeline. This is observability-only: the
                # execution decision still uses the Over bid/ask.
                book = dict(book)
                book.update(
                    {
                        "under_pair_available": True,
                        "under_best_bid": under_book.get("best_bid"),
                        "under_best_bid_size": under_book.get("best_bid_size"),
                        "under_best_ask": under_book.get("best_ask"),
                        "under_best_ask_size": under_book.get("best_ask_size"),
                        "under_ltp": under_book.get("ltp"),
                        "under_source": under_book.get("source"),
                        "under_latency_ms": under_book.get("latency_ms"),
                        "under_api_ts": under_book.get("api_ts"),
                    }
                )
            self._process_tick(game, market, payload, book, ask)

        self._settle_finished_games()
        self._flush_expired_score_confirmations()
        self._maybe_log_tick_buffer_health()
        self._log_runtime_debug_rollups()

    def _maybe_log_tick_buffer_health(self, interval_secs: float = 1800.0) -> None:
        now = _now_ts()
        if (now - float(getattr(self, "_last_tick_buffer_health_log_ts", 0.0) or 0.0)) < interval_secs:
            return
        self._last_tick_buffer_health_log_ts = now

        # When `active_games` is available (live monitor base class), only
        # treat states whose game is currently being polled as actionable for
        # WARN escalation. Without this filter, buffers from finished games
        # spam WARN every interval with growing newest_age (observed up to
        # 29,000s after the day's last game ended).
        active_games_map = getattr(self, "active_games", None)
        if isinstance(active_games_map, dict) and active_games_map:
            active_game_pks: Optional[set] = {
                int(gpk) for gpk, on in active_games_map.items() if on
            }
        else:
            # No active-games map (test stub or pre-monitor init) — fall back
            # to treating every state as actionable to preserve historical behavior.
            active_game_pks = None

        lengths = []
        newest_ages = []
        oldest_ages = []
        active_lengths = []
        active_newest_ages = []
        for (game_pk, _line), state in self._line_states.items():
            ticks = list(getattr(state, "tick_buffer", []) or [])
            lengths.append(len(ticks))
            is_active = active_game_pks is None or int(game_pk) in active_game_pks
            if is_active:
                active_lengths.append(len(ticks))
            if not ticks:
                continue
            ts_values = [
                float(t.get("ts"))
                for t in ticks
                if isinstance(t, dict) and t.get("ts") is not None
            ]
            if not ts_values:
                continue
            age = max(0.0, now - max(ts_values))
            newest_ages.append(age)
            oldest_ages.append(max(0.0, now - min(ts_values)))
            if is_active:
                active_newest_ages.append(age)

        if not lengths:
            LOGGER.debug("tick_buffer health: 0 line_states")
            return

        sorted_lengths = sorted(lengths)
        median_len = sorted_lengths[len(sorted_lengths) // 2]
        nonempty = sum(1 for n in lengths if n > 0)
        newest_age_max = max(newest_ages) if newest_ages else -1.0
        oldest_age_max = max(oldest_ages) if oldest_ages else -1.0

        # Abnormal only when we have at least one actively-polled line state and
        # *that* state is empty or stale. States from finished games are no longer
        # actionable; their staleness is expected.
        nonempty_active = sum(1 for n in active_lengths if n > 0)
        active_newest_age_max = max(active_newest_ages) if active_newest_ages else -1.0
        abnormal = (
            (len(active_lengths) > 0 and nonempty_active == 0)
            or (active_newest_age_max > 900.0)
        )
        level = logging.WARNING if abnormal else logging.DEBUG
        LOGGER.log(
            level,
            "tick_buffer health: %d line_states, %d nonempty, len min/median/max=%d/%d/%d, "
            "newest_age max=%.1fs, oldest_age max=%.1fs",
            len(lengths),
            nonempty,
            sorted_lengths[0],
            median_len,
            sorted_lengths[-1],
            newest_age_max,
            oldest_age_max,
        )

    def _log_runtime_debug_rollups(
        self, *, force: bool = False, interval_secs: float = 1800.0
    ) -> None:
        return _log_runtime_debug_rollups_impl(
            self,
            force=force,
            interval_secs=interval_secs,
        )

    # ------------------------------------------------------------------
    # Per-tick logic
    # ------------------------------------------------------------------

    def _process_tick(
        self,
        game: ScheduledGame,
        market: OUMarket,
        payload: dict,
        book: dict,
        ask: float,
    ) -> None:
        return _process_tick_impl(
            self=self,
            game=game,
            market=market,
            payload=payload,
            book=book,
            ask=ask,
            line_state_cls=LineState,
        )

    def _place_bet(
        self,
        game: ScheduledGame,
        market: OUMarket,
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
        # 2026-05-21 (P1c): correlated-line exposure cap. The check
        # was previously live-only (in live_engine_placement); lifting
        # it here so paper inherits the same protection. Paper version
        # counts every same-game over bet (no order_status filter
        # since paper bets don't carry one). LiveTradingEngine has its
        # own override that adds the live-specific status filter.
        correlated_skip = self._evaluate_correlated_line_cap(
            game=game, market=market
        )
        if correlated_skip is not None:
            self._last_place_bet_skip_reason = correlated_skip
            return None

        self._bet_counter += 1
        bet_id = f"{self.date_str}_{game.game_pk}_{market.line}_{self._bet_counter:04d}"

        inf_away = away_score_before + inferred_runs if batting_is_away else away_score_before
        inf_home = home_score_before if batting_is_away else home_score_before + inferred_runs
        state_diag = state_value_diagnostics or {}

        def _diag_float(name: str) -> Optional[float]:
            value = state_diag.get(name)
            if value is None or value == "":
                return None
            try:
                return float(value)
            except (TypeError, ValueError):
                return None

        def _diag_str(name: str) -> str:
            return str(state_diag.get(name) or "")

        def _diag_bool(name: str) -> Optional[bool]:
            value = state_diag.get(name)
            if value is None or value == "":
                return None
            if isinstance(value, bool):
                return value
            if isinstance(value, (int, float)):
                return bool(value)
            text = str(value).strip().lower()
            if text in {"true", "1", "yes", "y"}:
                return True
            if text in {"false", "0", "no", "n"}:
                return False
            return None

        def _diag_int(name: str) -> Optional[int]:
            value = state_diag.get(name)
            if value is None or value == "":
                return None
            try:
                return int(float(value))
            except (TypeError, ValueError):
                return None

        bet = BetRecord(
            bet_id=bet_id,
            placed_at=_now_iso(),
            game_pk=game.game_pk,
            away_abbrev=game.away_abbrev,
            home_abbrev=game.home_abbrev,
            line=market.line,
            side="over",
            entry_ask=best_ask,
            fair_value=fair_value,
            base_fair_value=base_fair_value,
            stage2_run_env_delta=round(stage2_run_env_delta, 4),
            team_offense_delta=round(team_offense_delta, 4),
            edge=edge,
            inferred_runs=inferred_runs,
            inning=inning,
            inning_state=inning_state,
            outs=outs,
            away_score_before=away_score_before,
            home_score_before=home_score_before,
            inferred_away_after=inf_away,
            inferred_home_after=inf_home,
            stake=self.trade_args.stake,
            runners_on=runners_on,
            venue_name=game.venue_name,
            ltp_at_signal=ltp,
            config_label=str(getattr(self.trade_args, "config_label", "default") or "default"),
            inferred_state_base_poisson=_diag_float("inferred_state_base_poisson"),
            inferred_state_base_empirical=_diag_float("inferred_state_base_empirical"),
            inferred_state_poisson_minus_empirical=_diag_float("inferred_state_poisson_minus_empirical"),
            inferred_state_empirical_edge=_diag_float("inferred_state_empirical_edge"),
            inferred_state_n=_diag_float("inferred_state_n"),
            inferred_state_n_samples=_diag_float("inferred_state_n_samples"),
            inferred_state_weighted_n=_diag_float("inferred_state_weighted_n"),
            inferred_state_effective_n=_diag_float("inferred_state_effective_n"),
            inferred_state_effective_n_proxy=_diag_float("inferred_state_effective_n_proxy"),
            inferred_state_stage1_trust_weight=_diag_float("inferred_state_stage1_trust_weight"),
            inferred_state_stage1_support_bucket=_diag_str("inferred_state_stage1_support_bucket"),
            inferred_state_exact_cell_support=_diag_bool("inferred_state_exact_cell_support"),
            inferred_state_poisson_line_exact=_diag_bool("inferred_state_poisson_line_exact"),
            inferred_state_empirical_line_exact=_diag_bool("inferred_state_empirical_line_exact"),
            inferred_state_empirical_sample_support=_diag_float("inferred_state_empirical_sample_support"),
            inferred_state_empirical_sample_bucket=_diag_str("inferred_state_empirical_sample_bucket"),
            inferred_state_state_fallback_penalty=_diag_float("inferred_state_state_fallback_penalty"),
            inferred_state_line_fallback_penalty=_diag_float("inferred_state_line_fallback_penalty"),
            inferred_state_fallback_level=_diag_int("inferred_state_fallback_level"),
            inferred_state_fallback_label=_diag_str("inferred_state_fallback_label"),
            inferred_state_cell_key=_diag_str("inferred_state_cell_key"),
            inferred_state_line_key_poisson=_diag_str("inferred_state_line_key_poisson"),
            inferred_state_line_key_empirical=_diag_str("inferred_state_line_key_empirical"),
            inferred_state_line_fallback_mode=_diag_str("inferred_state_line_fallback_mode"),
            inferred_state_empirical_line_fallback_mode=_diag_str("inferred_state_empirical_line_fallback_mode"),
            inferred_state_empirical_line_source_key=_diag_str("inferred_state_empirical_line_source_key"),
            inferred_state_empirical_line_source_key_low=_diag_str("inferred_state_empirical_line_source_key_low"),
            inferred_state_empirical_line_source_key_high=_diag_str("inferred_state_empirical_line_source_key_high"),
            inferred_state_used_fallback=_diag_bool("inferred_state_used_fallback"),
            inferred_state_base_source=_diag_str("inferred_state_base_source"),
            home_leading_late=_diag_bool("home_leading_late"),
            batting_team_is_home=_diag_bool("batting_team_is_home"),
            bottom9_available_if_needed=_diag_bool("bottom9_available_if_needed"),
            expected_remaining_half_innings=_diag_float("expected_remaining_half_innings"),
            expected_remaining_pa_bucket=_diag_str("expected_remaining_pa_bucket"),
            home_skip_bottom9_risk=_diag_float("home_skip_bottom9_risk"),
            scoring_path_available=_diag_bool("scoring_path_available"),
            scoring_path_innings_observed=_diag_int("scoring_path_innings_observed"),
            scoring_path_runs_observed=_diag_int("scoring_path_runs_observed"),
            scoring_path_inning_runs=_diag_str("scoring_path_inning_runs"),
            scoring_inning_rate=_diag_float("scoring_inning_rate"),
            scoring_half_rate=_diag_float("scoring_half_rate"),
            burst_share=_diag_float("burst_share"),
            scoreless_streak=_diag_int("scoreless_streak"),
            recent2_run_share=_diag_float("recent2_run_share"),
            weighted_run_inning_norm=_diag_float("weighted_run_inning_norm"),
            inning_run_slope=_diag_float("inning_run_slope"),
            state_value_strategy=_diag_str("state_value_strategy"),
            current_state_value_edge=_diag_float("current_state_value_edge"),
            current_state_value_fv_raw=_diag_float("current_state_value_fv_raw"),
            current_state_value_empirical_edge=_diag_float("current_state_value_empirical_edge"),
            current_state_value_effective_n_proxy=_diag_float("current_state_value_effective_n_proxy"),
            current_state_value_stage1_trust_weight=_diag_float("current_state_value_stage1_trust_weight"),
            current_state_value_stage1_support_bucket=_diag_str("current_state_value_stage1_support_bucket"),
            current_state_value_exact_cell_support=_diag_bool("current_state_value_exact_cell_support"),
            current_state_value_poisson_line_exact=_diag_bool("current_state_value_poisson_line_exact"),
            current_state_value_empirical_line_exact=_diag_bool("current_state_value_empirical_line_exact"),
            current_state_value_empirical_sample_support=_diag_float("current_state_value_empirical_sample_support"),
            current_state_value_empirical_sample_bucket=_diag_str("current_state_value_empirical_sample_bucket"),
            current_state_value_state_fallback_penalty=_diag_float("current_state_value_state_fallback_penalty"),
            current_state_value_line_fallback_penalty=_diag_float("current_state_value_line_fallback_penalty"),
            shadow_fv_inferred_lift=_diag_float("shadow_fv_inferred_lift"),
            shadow_no_event_edge=_diag_float("shadow_no_event_edge"),
            shadow_after_event_edge=_diag_float("shadow_after_event_edge"),
            shadow_p_score_event_proxy=_diag_float("shadow_p_score_event_proxy"),
            shadow_phantom_risk_score=_diag_float("shadow_phantom_risk_score"),
            shadow_phantom_risk_band=_diag_str("shadow_phantom_risk_band"),
            shadow_transition_model=_diag_str("shadow_transition_model"),
            shadow_low_ask_high_edge=_diag_bool("shadow_low_ask_high_edge"),
            shadow_runs_needed_exact_3p5=_diag_bool("shadow_runs_needed_exact_3p5"),
            shadow_current_state_edge_bucket=_diag_str("shadow_current_state_edge_bucket"),
            shadow_phantom_risk_bucket=_diag_str("shadow_phantom_risk_bucket"),
            shadow_current_phantom_combo_bucket=_diag_str("shadow_current_phantom_combo_bucket"),
            shadow_inning_bucket=_diag_str("shadow_inning_bucket"),
            shadow_inning_runs_needed_bucket=_diag_str("shadow_inning_runs_needed_bucket"),
            shadow_bottom9_home_lead_context=_diag_str("shadow_bottom9_home_lead_context"),
            shadow_home_skip_bottom9_risk_bucket=_diag_str("shadow_home_skip_bottom9_risk_bucket"),
        )
        self._bets.append(bet)

        half = _inning_state_to_half(inning_state)
        batting_team = game.away_abbrev if half == "T" else game.home_abbrev
        current_total = away_score_before + home_score_before
        pace_per_9 = (current_total / inning) * 9 if inning > 0 else 0
        s2_note = f" S2={stage2_run_env_delta:+.3f}" if abs(stage2_run_env_delta) >= 0.01 else ""
        s3_note = f" S3={team_offense_delta:+.3f}" if abs(team_offense_delta) >= 0.01 else ""
        adj_note = (f"  [base={base_fair_value:.3f}{s2_note}{s3_note}]"
                    if (abs(stage2_run_env_delta) >= 0.01 or abs(team_offense_delta) >= 0.01)
                    else "")
        ltp_note = ""
        if ltp is not None:
            ltp_gap = best_ask - ltp  # positive = entry above LTP (adverse selection risk)
            ltp_note = f"  ltp={ltp:.3f}(gap={ltp_gap:+.3f})"
        LOGGER.info(
            "PAPER BET [%s] %s@%s O/U %.1f OVER @ %.3f%s  FV=%.3f  edge=%.3f%s  "
            "inn=%s%d outs=%d  %s inferred +%d runs  "
            "score=%d-%d(total=%d pace=%.1f/9)  stake=$%.0f",
            bet_id,
            game.away_abbrev, game.home_abbrev, float(market.line),
            best_ask, ltp_note, fair_value, edge, adj_note,
            half, inning, outs,
            batting_team, inferred_runs,
            away_score_before, home_score_before, current_total, pace_per_9,
            self.trade_args.stake,
        )
        self._save_session()
        return bet

    # ------------------------------------------------------------------
    # Capture surfaces (sidecar IO for placed bets and skip-with-features).
    # Implementations live in capture_helpers.py; methods below are thin
    # delegators preserved on the class so test stubs and any future
    # subclass overrides continue to work.
    # ------------------------------------------------------------------

    def _fetch_depth_snapshot(self, token_id: str, depth: int) -> dict:
        return _fetch_depth_snapshot_impl(self, token_id, depth)

    def _start_book_capture(
        self,
        bet: "BetRecord",
        token_id: str,
        initial_book: dict,
        signal_ts: float,
    ) -> None:
        return _start_book_capture_impl(self, bet, token_id, initial_book, signal_ts)

    def _start_tape_capture(
        self,
        *,
        bet_id: str,
        token_id: str,
        signal_ts: float,
        current_ask: Optional[float],
    ) -> Dict[str, object]:
        return _start_tape_capture_impl(
            self,
            bet_id=bet_id,
            token_id=token_id,
            signal_ts=signal_ts,
            current_ask=current_ask,
        )

    def _start_family_b_capture(
        self,
        *,
        bet_id: str,
        token_id: str,
        limit_price: Optional[float],
        depth: int = 5,
    ) -> Dict[str, object]:
        return _start_family_b_capture_impl(
            self,
            bet_id=bet_id,
            token_id=token_id,
            limit_price=limit_price,
            depth=depth,
        )

    def _start_family_c_capture(
        self,
        *,
        bet_id: str,
        line_state: "LineState",
        signal_ts: float,
    ) -> Dict[str, object]:
        return _start_family_c_capture_impl(
            self,
            bet_id=bet_id,
            line_state=line_state,
            signal_ts=signal_ts,
        )

    # ------------------------------------------------------------------
    # Settlement
    # ------------------------------------------------------------------

    def _is_bet_executable(self, bet: BetRecord) -> bool:
        """Returns True if this bet represents a position that should be settled with P&L.

        Paper trading: always True â€” every paper bet is a simulated execution.
        Live trading: overridden in RealTradingEngine to return
            bet.order_status == "filled", so that cancelled/unfilled orders
            are recorded as missed signals with zero P&L rather than phantom wins.
        """
        return True

    def _settle_finished_games(self) -> None:
        unsettled = [b for b in self._bets if not b.settled]
        session_changed = False

        for bet in unsettled:
            bet_id = getattr(bet, "bet_id", "")
            game = self.games.get(bet.game_pk)
            if game is None:
                continue
            if not game.is_final():
                continue
            if game.score.away is None or game.score.home is None:
                continue

            final_away = game.score.away
            final_home = game.score.home
            final_total = final_away + final_home
            line_val = float(bet.line)
            counterfactual_won = final_total > line_val

            # Always record game outcome fields â€” useful for signal quality analysis
            # regardless of whether the bet was executed.
            bet.settled = True
            bet.settled_at = _now_iso()
            bet.final_away = final_away
            bet.final_home = final_home
            bet.final_total = final_total

            key = (bet.game_pk, bet.line)
            state = self._line_states.get(key)
            if state:
                state.bet_open = False

            if self._is_bet_executable(bet):
                # Executed position â€” compute real P&L
                effective_stake = float(bet.stake)
                settlement_price = float(getattr(bet, "entry_ask", 0.0) or 0.0)
                live_fill_shares = None
                if getattr(bet, "order_status", "") == "filled":
                    fill_size = getattr(bet, "fill_size", None)
                    # Live mode stores explicit execution lineage fields.
                    # Use actual_fill_price (or fill_price fallback) for realized P&L.
                    actual_fill_price = getattr(bet, "actual_fill_price", None)
                    fill_price = getattr(bet, "fill_price", None)
                    if actual_fill_price is not None:
                        settlement_price = float(actual_fill_price)
                    elif fill_price is not None:
                        settlement_price = float(fill_price)
                    if fill_size is not None and float(fill_size) > 0:
                        live_fill_shares = float(fill_size)
                        if settlement_price > 0:
                            effective_stake = round(live_fill_shares * settlement_price, 2)
                            if hasattr(bet, "fill_cost"):
                                bet.fill_cost = effective_stake
                            if hasattr(bet, "filled_shares"):
                                bet.filled_shares = live_fill_shares
                            if hasattr(bet, "fill_cost_usdc"):
                                bet.fill_cost_usdc = effective_stake

                if settlement_price <= 0:
                    LOGGER.warning(
                        "Invalid settlement price for bet %s (price=%s) - using entry_ask fallback",
                        bet_id,
                        settlement_price,
                    )
                    settlement_price = float(getattr(bet, "entry_ask", 0.0) or 0.0)

                if settlement_price <= 0:
                    LOGGER.warning(
                        "Bet %s has non-positive settlement price after fallback; forcing zero payout",
                        bet_id,
                    )
                    payout = 0.0
                elif live_fill_shares is not None:
                    payout = round(live_fill_shares, 2) if counterfactual_won else 0.0
                else:
                    payout = round(effective_stake / settlement_price, 2) if counterfactual_won else 0.0
                profit = round(payout - effective_stake, 2)
                bet.won    = counterfactual_won
                bet.payout = payout
                bet.profit = profit
                if hasattr(bet, "payout_usdc"):
                    bet.payout_usdc = payout
                LOGGER.info(
                    "SETTLED [%s] %s@%s O/U %s OVER  final=%d-%d (total=%d vs line=%.1f)  "
                    "%s  profit=$%.2f",
                    bet_id,
                    bet.away_abbrev, bet.home_abbrev, bet.line,
                    final_away, final_home, final_total, line_val,
                    "WON" if counterfactual_won else "LOST", profit,
                )
            else:
                # Unfilled order â€” zero P&L, but record counterfactual outcome
                # so signal quality can be measured separately from execution quality.
                bet.won    = counterfactual_won   # would have won if filled
                bet.payout = 0.0
                bet.profit = 0.0
                if hasattr(bet, "payout_usdc"):
                    bet.payout_usdc = 0.0
                LOGGER.info(
                    "MISSED  [%s] %s@%s O/U %s OVER  final=%d-%d (total=%d vs line=%.1f)  "
                    "counterfactual=%s  order never filled â€” zero P&L",
                    bet_id,
                    bet.away_abbrev, bet.home_abbrev, bet.line,
                    final_away, final_home, final_total, line_val,
                    "WIN" if counterfactual_won else "LOSS",
                )

            self._append_to_ledger(bet)
            session_changed = True

        # --- Outcome records for shadow analysis ---
        # For every finalized game with tracked markets, write one outcome record
        # per (game, line) so blocked candidate rows can be joined to final scores.
        # This loop is independent of bet placement â€” it runs even for games where
        # every signal was blocked and no bet was placed.
        for game_pk, game in list(self.games.items()):
            if not game.is_final():
                continue
            if game.score.away is None or game.score.home is None:
                continue
            if game_pk not in self.matches:
                continue
            final_away = game.score.away
            final_home = game.score.home
            for market in self.matches[game_pk].markets:
                key = (game_pk, market.line)
                if key not in self._outcome_games_written:
                    self._write_outcome_record(
                        game_pk=game_pk,
                        line=market.line,
                        final_away=final_away,
                        final_home=final_home,
                    )
                    self._outcome_games_written.add(key)
                    session_changed = True

        if session_changed:
            self._save_session()

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _append_to_ledger(self, bet: BetRecord) -> None:
        with open(self._ledger_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(asdict(bet)) + "\n")

    def _save_session(self) -> None:
        # Payload assembly lives in session_serialization.build_paper_session_payload;
        # this method owns the file write + candidate-rollup write side effects.
        self._flush_expired_score_confirmations()
        session = _build_paper_session_payload(self)
        with open(self._session_path, "w", encoding="utf-8") as f:
            json.dump(session, f, indent=2)
        self._write_candidate_rollup()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_trade_args(argv=None) -> Tuple[argparse.Namespace, argparse.Namespace]:
    """Backward-compatible wrapper around signal_config.parse_trade_args."""
    return _parse_trade_args_impl(argv)


def _check_refresh_freshness(
    date_str: str,
    *,
    refresh_dir: Path = None,
    warn_hours: float = 36.0,
    alert_hours: float = 60.0,
) -> Dict[str, Any]:
    """Return a freshness snapshot of the most-recent startup_refresh
    artifact relative to `date_str`.

    Companion to `human_review.system_health._refresh_staleness_health`
    -- same date-from-filename parsing so the boot log and the daily
    review block always agree on what "stale" means. Skips `_plan`
    artifacts (dry-runs) and future-dated artifacts (data anomalies
    like the stale 2026-06-15 plan caught by the 2026-05-22 audit).

    Status values: `ok` (< warn_hours), `warn` (warn..alert),
    `alert` (>= alert), `no_refresh_artifacts`, `all_future_dated`,
    `check_error`.
    """
    if refresh_dir is None:
        refresh_dir = (
            Path(__file__).resolve().parents[2]
            / "data" / "analysis_output" / "startup_refresh"
        )
    snap: Dict[str, Any] = {
        "artifact_dir": str(refresh_dir),
        "session_date": date_str,
        "thresholds": {"warn_hours": warn_hours, "alert_hours": alert_hours},
        "last_refresh_date": None,
        "last_refresh_artifact": None,
        "hours_since_last_refresh": None,
        "status": "check_error",
    }
    if not refresh_dir.exists():
        snap["error"] = "startup_refresh directory missing"
        return snap
    dated = []
    for f in refresh_dir.glob("*_startup_refresh.json"):
        name = f.name
        if "_plan" in name:
            continue
        slug = name.split("_")[0]
        if len(slug) == 10 and slug[4] == "-" and slug[7] == "-":
            dated.append((slug, f))
    if not dated:
        snap["status"] = "no_refresh_artifacts"
        return snap
    dated.sort(key=lambda x: x[0])
    valid = [(d, f) for d, f in dated if d <= date_str]
    if not valid:
        snap["status"] = "all_future_dated"
        return snap
    # Defense: a plan-only artifact (body `plan_only: True` with
    # `steps_ok: 0`) can sneak in without the `_plan` filename suffix
    # -- see the misnamed 2026-06-15 file the 2026-05-22 audit caught.
    # The filename filter above won't catch those. Walk newest->oldest
    # and pick the newest artifact whose body confirms an executed
    # refresh (plan_only != True AND steps_ok > 0). Falls back to the
    # newest dated artifact if no body-validated entry is found, so
    # the function never returns silently empty when files exist.
    import json as _json
    last_date, last_file = None, None
    for d, f in reversed(valid):
        try:
            with f.open(encoding="utf-8") as _fp:
                body = _json.load(_fp)
        except (OSError, ValueError):
            continue
        if body.get("plan_only"):
            continue
        if int(body.get("steps_ok") or 0) <= 0:
            continue
        last_date, last_file = d, f
        break
    if last_file is None:
        last_date, last_file = valid[-1]
    snap["last_refresh_date"] = last_date
    snap["last_refresh_artifact"] = last_file.name
    try:
        from datetime import datetime as _dt2
        sd = _dt2.fromisoformat(date_str)
        ld = _dt2.fromisoformat(last_date)
        hours = max(0.0, (sd - ld).total_seconds() / 3600.0)
    except (ValueError, TypeError) as exc:
        snap["error"] = f"date parse failed: {exc}"
        return snap
    snap["hours_since_last_refresh"] = round(hours, 1)
    if hours >= alert_hours:
        snap["status"] = "alert"
    elif hours >= warn_hours:
        snap["status"] = "warn"
    else:
        snap["status"] = "ok"
    return snap


def _log_refresh_freshness(snap: Dict[str, Any], *, phase: str) -> None:
    """Log the freshness snapshot at the severity that matches its status."""
    status = snap.get("status", "unknown")
    last = snap.get("last_refresh_date") or "none"
    age = snap.get("hours_since_last_refresh")
    age_str = f"{age:.1f}h" if isinstance(age, (int, float)) else "n/a"
    thresholds = snap.get("thresholds", {})
    warn_h = thresholds.get("warn_hours", 36.0)
    alert_h = thresholds.get("alert_hours", 60.0)
    msg = (
        f"refresh-freshness {phase}: status={status} last={last} "
        f"age={age_str} (warn>={warn_h:.0f}h alert>={alert_h:.0f}h)"
    )
    if status == "alert":
        LOGGER.error(msg)
    elif status in ("warn", "no_refresh_artifacts", "all_future_dated"):
        LOGGER.warning(msg)
    elif status == "check_error":
        LOGGER.warning(msg + f" error={snap.get('error')}")
    else:
        LOGGER.info(msg)


def _log_mode_lever_summary(trade_args) -> Dict[str, str]:
    """Boot-time one-line summary of which CLI-flag mode levers are
    in enforce / shadow / off / ab. Returns the snapshot dict for
    tests / callers.

    Caught by 2026-05-23 audit: calibrator-enforce + scoped-Alt-A-
    enforce both shipped to production on 5/22 silently — both are
    CLI-flag levers so they don't write to promotion_events.jsonl,
    and (before this) didn't surface in session params either. The
    operator's terminal had no obvious sign that two behavioral
    promotions were live. This log line makes every promotion
    visible at every boot, and tags the line with "ENFORCE
    PROMOTIONS ACTIVE" when any lever is in enforce mode so it's
    easy to grep for.

    Mode levers covered (all paper-relevant; live adds its own):
      - prob_calibration_mode
      - stage1_shadow_empirical_mode
      - stage1_alt_a_scope_mode
      - gate_blowout_relax_mode
      - gate_min_current_total_relax_mode
      - under_emission_mode
    """
    levers = (
        "prob_calibration_mode",
        "stage1_shadow_empirical_mode",
        "stage1_alt_a_scope_mode",
        "gate_blowout_relax_mode",
        "gate_min_current_total_relax_mode",
        "under_emission_mode",
    )
    snapshot: Dict[str, str] = {}
    for name in levers:
        val = getattr(trade_args, name, None)
        if val is None:
            continue
        snapshot[name] = str(val).strip().lower() or "off"

    by_mode: Dict[str, list] = {"enforce": [], "shadow": [], "ab": [], "off": []}
    for name, val in snapshot.items():
        # Strip the trailing "_mode" for display brevity.
        display = name[:-5] if name.endswith("_mode") else name
        bucket = val if val in by_mode else "off"
        by_mode[bucket].append(display)

    def _fmt(names):
        return ", ".join(sorted(names)) if names else "(none)"

    if by_mode["enforce"]:
        prefix = "ENFORCE PROMOTIONS ACTIVE"
        log_fn = LOGGER.warning  # WARN so it's visible above INFO chatter
    else:
        prefix = "Mode levers"
        log_fn = LOGGER.info

    parts = [f"enforce={_fmt(by_mode['enforce'])}"]
    if by_mode["ab"]:
        parts.append(f"ab={_fmt(by_mode['ab'])}")
    parts.append(f"shadow={_fmt(by_mode['shadow'])}")
    parts.append(f"off={_fmt(by_mode['off'])}")
    log_fn("%s | %s", prefix, " | ".join(parts))
    return snapshot


def _check_gate_threshold_drift(trade_args) -> Dict[str, Any]:
    """Boot-time check: are enforced-gate thresholds set looser than the
    codebase default?

    Caught by the 2026-05-22 deep audit: the trailing-30d cohort showed
    17 bets at edge>=0.22 losing -$190 in live mode. Investigation found
    the active `extreme_edge_max` was 0.30 (TR17 default) for the 4/18-
    5/04 placements; TR19 tightened the default to 0.22 on 5/08 and
    those bets correctly stopped. The audit incorrectly attributed the
    cohort to a gate bug because the bucket label `>=0.22` matches the
    current default but doesn't tell you what threshold was active at
    placement time. This check prevents a recurrence: if the operator's
    saved CLI command pins a looser value than the codebase default,
    log a WARN at boot so they see it before the day runs.

    Returns a snapshot dict for logging / testing. Each entry is
    `{ "looser_than_default": bool, "runtime": float, "default": float,
       "looseness_pp": float }`. Empty dict if no drift.
    """
    from signal_config import (
        DEFAULT_EXTREME_EDGE_MAX,
        DEFAULT_EDGE_THRESHOLD,
        DEFAULT_EDGE_THRESHOLD_HIGH_LINE,
        DEFAULT_MIN_ENTRY_ASK,
        DEFAULT_MIN_ENTRY_ASK_HIGH_LINE,
        DEFAULT_MIN_INNING,
        DEFAULT_MIN_INNING_HIGH_LINE,
        DEFAULT_RUNS_NEEDED_MAX,
        DEFAULT_MIN_CURRENT_TOTAL,
    )
    # For each gate, "looser" means a value that would let MORE bets
    # through. Per-gate direction:
    #   - extreme_edge_max: looser = larger (raises the cap)
    #   - edge_threshold(_high_line): looser = smaller (lowers the min)
    #   - min_entry_ask(_high_line): looser = smaller (cheaper bets pass)
    #   - min_inning(_high_line): looser = smaller (earlier innings pass)
    #   - runs_needed_max: looser = larger (more bets pass)
    #   - min_current_total: looser = smaller (lower-total games pass)
    gates = [
        ("extreme_edge_max", DEFAULT_EXTREME_EDGE_MAX, "larger"),
        ("edge_threshold", DEFAULT_EDGE_THRESHOLD, "smaller"),
        ("edge_threshold_high_line", DEFAULT_EDGE_THRESHOLD_HIGH_LINE, "smaller"),
        ("min_entry_ask", DEFAULT_MIN_ENTRY_ASK, "smaller"),
        ("min_entry_ask_high_line", DEFAULT_MIN_ENTRY_ASK_HIGH_LINE, "smaller"),
        ("min_inning", DEFAULT_MIN_INNING, "smaller"),
        ("min_inning_high_line", DEFAULT_MIN_INNING_HIGH_LINE, "smaller"),
        ("runs_needed_max", DEFAULT_RUNS_NEEDED_MAX, "larger"),
        ("min_current_total", DEFAULT_MIN_CURRENT_TOTAL, "smaller"),
    ]
    drift: Dict[str, Any] = {}
    for name, default, direction in gates:
        runtime = getattr(trade_args, name, None)
        if runtime is None:
            continue
        try:
            runtime_f = float(runtime)
            default_f = float(default)
        except (TypeError, ValueError):
            continue
        if direction == "larger":
            looser = runtime_f > default_f
            pp = round(runtime_f - default_f, 4)
        else:
            looser = runtime_f < default_f
            pp = round(default_f - runtime_f, 4)
        if looser:
            drift[name] = {
                "runtime": runtime_f,
                "default": default_f,
                "looseness_pp": pp,
                "direction": direction,
            }
    return drift


def _log_gate_threshold_drift(drift: Dict[str, Any]) -> None:
    """Log gate-drift WARN entries -- one per drifted gate."""
    if not drift:
        LOGGER.info("Gate thresholds: all match codebase defaults.")
        return
    LOGGER.warning(
        "Gate-threshold drift: %d gate(s) are set LOOSER than the "
        "codebase default. The codebase defaults reflect the most "
        "recently-tuned thresholds (e.g. TR19's 0.22 extreme_edge_max). "
        "If your saved CLI command pins an older value, recent walk-"
        "forward evidence may say to tighten -- review and update.",
        len(drift),
    )
    for name, info in sorted(drift.items()):
        LOGGER.warning(
            "  drift: --%s runtime=%s default=%s (%s by %s)",
            name.replace("_", "-"),
            info["runtime"], info["default"],
            "looser" if info["direction"] == "larger" else "looser",
            info["looseness_pp"],
        )


def _run_paper_startup_refresh(date_str: str, trade_args) -> None:
    """2026-05-21 (P1b followup): run the daily-refresh pipeline before
    paper engine boots. Mirrors live's startup-refresh wiring
    (see live_engine_setup.run_startup_refresh) minus the live-only
    fields (daily_budget, per_game_budget_fraction). Without this,
    paper-only days skip the refresh entirely and the calibrator /
    drift report / training table silently go stale -- caught by the
    2026-05-21 audit when the engine ran 2 days on stale artifacts.

    Default enabled (`--startup-refresh`); operator can opt out via
    `--no-startup-refresh`. Fail-open unless `--startup-refresh-strict`.
    """
    if not getattr(trade_args, "startup_refresh", True):
        LOGGER.info("Paper startup refresh disabled by --no-startup-refresh.")
        return
    try:
        from run_daily_refresh import (
            RefreshConfig as _StartupRefreshConfig,
            run_startup_refresh as _run_startup_refresh_impl,
        )
    except ImportError:
        LOGGER.warning(
            "run_daily_refresh not importable; skipping paper startup refresh."
        )
        return

    strict = bool(getattr(trade_args, "startup_refresh_strict", False))
    config = _StartupRefreshConfig(
        active_date=date_str,
        strict=strict,
        stake=float(getattr(trade_args, "stake", 10.0) or 10.0),
    )
    LOGGER.info(
        "Paper startup refresh: active_date=%s strict=%s "
        "(disable with --no-startup-refresh)",
        date_str, strict,
    )
    try:
        payload = _run_startup_refresh_impl(config)
    except Exception:
        if strict:
            LOGGER.exception(
                "Paper startup refresh failed in strict mode; aborting."
            )
            raise
        LOGGER.exception(
            "Paper startup refresh failed; continuing (fail-open). "
            "Daily review's refresh_staleness_health block will flag "
            "the resulting artifact age."
        )
        return

    LOGGER.info(
        "Paper startup refresh complete: max_refresh_date=%s "
        "steps_ok=%s steps_failed=%s manifest=%s",
        payload.get("max_refresh_date") or "none",
        payload.get("steps_ok"),
        payload.get("steps_failed"),
        payload.get("manifest_path"),
    )
    if payload.get("steps_failed"):
        LOGGER.warning(
            "Paper startup refresh had failures; inspect manifest "
            "before trusting refreshed artifacts."
        )


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    trade_args, monitor_args = parse_trade_args()

    if not trade_args.cache_path.exists():
        LOGGER.error("Cache not found: %s", trade_args.cache_path)
        sys.exit(1)

    # 2026-05-22 (audit followup): gate-threshold-drift WARN. Caught
    # the 4/18-5/04 extreme_edge_max=0.30 era that polluted the
    # trailing-30d cohort report. Fires at boot so the operator sees
    # any saved CLI flag that's looser than today's codebase default
    # before the day's trading runs.
    _log_gate_threshold_drift(_check_gate_threshold_drift(trade_args))

    # 2026-05-23 (audit followup): mode-lever summary. Caught when
    # calibrator-enforce + scoped-Alt-A-enforce both flipped on 5/22
    # with no visible audit trail. One WARN-tier line at boot tells
    # the operator which CLI-flag levers are in enforce vs shadow.
    _log_mode_lever_summary(trade_args)

    # Resolve the active date the same way SignalEngine.__init__ does.
    # We need it before the engine is constructed so the refresh sees
    # the right active_date.
    from zoneinfo import ZoneInfo
    from datetime import datetime as _dt
    try:
        _tz = ZoneInfo(getattr(monitor_args, "timezone", "America/New_York"))
    except Exception:
        _tz = None
    if monitor_args.date:
        _refresh_date = monitor_args.date
    elif _tz is not None:
        _refresh_date = _dt.now(_tz).strftime("%Y-%m-%d")
    else:
        _refresh_date = _dt.now().strftime("%Y-%m-%d")

    # 2026-05-22 (audit followup): stale --date detection. Caught when
    # the operator launched today (5/22) with --date 2026-05-21 baked
    # into the saved CLI command; the bot ran the daily refresh for
    # 20+ minutes, then loaded yesterday's all-final schedule, then
    # exited with "all scheduled games are final". This WARN fires
    # in seconds so the operator can Ctrl-C and re-launch with the
    # right date before the refresh burns time.
    if monitor_args.date and _tz is not None:
        try:
            _today_str = _dt.now(_tz).strftime("%Y-%m-%d")
            if monitor_args.date < _today_str:
                _days_back = (
                    _dt.strptime(_today_str, "%Y-%m-%d")
                    - _dt.strptime(monitor_args.date, "%Y-%m-%d")
                ).days
                LOGGER.warning(
                    "STALE --date FLAG: --date=%s is %d day(s) in the past "
                    "(today is %s). This is fine for backfill/replay, but if "
                    "you meant to run on TODAY'S games, Ctrl-C now and re-"
                    "launch without --date (or with --date %s). The startup "
                    "refresh below will run for ~20min before the engine "
                    "loads what is likely an all-final schedule.",
                    monitor_args.date, _days_back, _today_str, _today_str,
                )
            elif monitor_args.date > _today_str:
                LOGGER.warning(
                    "FUTURE --date FLAG: --date=%s is after today (%s). "
                    "Schedule fetch will return no games; engine will exit "
                    "early.",
                    monitor_args.date, _today_str,
                )
        except (TypeError, ValueError):
            pass

    # 2026-05-22 (audit followup): boot-time refresh-freshness heartbeat.
    # Snapshot pre, run refresh, snapshot post. Logged at INFO/WARN/ERROR
    # by status. If --require-fresh-refresh and post is still alert-stale,
    # abort -- catches the case where the refresh attempt itself succeeded
    # but produced an artifact older than threshold (e.g. forced active_date).
    _max_age_h = float(getattr(trade_args, "max_refresh_age_hours", 60.0) or 60.0)
    _snap_pre = _check_refresh_freshness(_refresh_date, alert_hours=_max_age_h)
    _log_refresh_freshness(_snap_pre, phase="pre-refresh")
    _run_paper_startup_refresh(date_str=_refresh_date, trade_args=trade_args)
    _snap_post = _check_refresh_freshness(_refresh_date, alert_hours=_max_age_h)
    _log_refresh_freshness(_snap_post, phase="post-refresh")
    if (
        getattr(trade_args, "require_fresh_refresh", False)
        and _snap_post.get("status") == "alert"
    ):
        LOGGER.error(
            "Aborting: --require-fresh-refresh is set and the post-refresh "
            "freshness check is still alert-stale (%.1fh >= %.0fh). Last "
            "refresh artifact: %s. Run "
            "`python scripts/analysis/run_daily_refresh.py` manually or "
            "drop --require-fresh-refresh to proceed.",
            _snap_post.get("hours_since_last_refresh") or -1.0,
            _max_age_h,
            _snap_post.get("last_refresh_artifact"),
        )
        sys.exit(1)

    LOGGER.info(
        "Starting paper trader (TR5)  "
        "edge=%.2f(std)/%.2f(hi)  jump=%.2f  confirm=%d ticks  "
        "ask=%.2f(std)/%.2f(hi)  runs_needed_max=%.1f  min_total=%d  spread<=%.2f  "
        "inning>=%d  high_line(>=%.1f)_inning>=%d  "
        "dedup=%.0fs  inning_dedup=%d(+%.2f edge)  stake=$%.0f",
        trade_args.edge_threshold, trade_args.edge_threshold_high_line,
        trade_args.jump_threshold,
        trade_args.confirmation_ticks,
        trade_args.min_entry_ask, trade_args.min_entry_ask_high_line,
        trade_args.runs_needed_max,
        trade_args.min_current_total, trade_args.max_spread,
        trade_args.min_inning, trade_args.high_line_cutoff,
        trade_args.min_inning_high_line,
        trade_args.event_dedup_secs,
        trade_args.inning_dedup_gap, trade_args.inning_dedup_edge_gap,
        trade_args.stake,
    )

    engine = SignalEngine(args=monitor_args, trade_args=trade_args)
    try:
        engine.run()
    except KeyboardInterrupt:
        LOGGER.info("Interrupted.")
    finally:
        # Grace-period settlement sweep: if any unsettled bets exist on games
        # that were in inning >= 8 (near end), poll for up to 5 minutes so we
        # don't leave late-inning wins unsettled just because the process ended
        # one minute before the game finished (see TR4 TEX@LAD incident).
        unsettled = [b for b in engine._bets if not b.settled]
        late_unsettled = [
            b for b in unsettled
            if b.inning >= 8
        ]
        if late_unsettled:
            LOGGER.info(
                "Grace-period settlement: %d late-inning unsettled bet(s). "
                "Polling up to 5 minutes for final scores...",
                len(late_unsettled),
            )
            deadline = time.time() + 300  # 5 minutes
            while time.time() < deadline:
                try:
                    payload = engine.stats_client.fetch_schedule_payload(engine.date_str)
                    fresh_games = engine.stats_client.parse_games(payload)
                    for game_pk, game in fresh_games.items():
                        engine.games[game_pk] = game
                    engine._settle_finished_games()
                except Exception as exc:
                    LOGGER.warning("Grace-period poll error: %s", exc)
                still_unsettled = [b for b in late_unsettled if not b.settled]
                if not still_unsettled:
                    LOGGER.info("All late-inning bets settled in grace period.")
                    break
                LOGGER.info(
                    "Grace period: %d bet(s) still pending. Waiting 30s...",
                    len(still_unsettled),
                )
                time.sleep(30)
            else:
                LOGGER.info("Grace period expired. Some bets may remain unsettled.")
        LOGGER.info("Saving final session state.")
        engine._save_session()


if __name__ == "__main__":
    main()



