#!/usr/bin/env python3
"""launch_parallel_engines.py -- Run parallel paper configs.

MVP goal: launch several paper SignalEngine processes against the same live
market day, each with its own paper_root and config_label. This lets us compare
configuration choices under the same baseball/market regime without refactoring
the engine into a single-process dispatcher.

The launcher owns startup refresh. Child engines always receive
--no-startup-refresh so N configs do not rebuild the same artifacts N times.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import signal
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - Python 3.8 fallback only
    ZoneInfo = None  # type: ignore[assignment]


PROJECT_DIR = Path(__file__).resolve().parents[2]
TRADING_DIR = PROJECT_DIR / "scripts" / "trading"
MONITOR_DIR = PROJECT_DIR / "scripts" / "monitor"
ANALYSIS_DIR = PROJECT_DIR / "scripts" / "analysis"
DEFAULT_SHARED_OUTPUT_ROOT = PROJECT_DIR / "data" / "polymarket" / "mlb_ou"

for _p in (str(TRADING_DIR), str(MONITOR_DIR), str(ANALYSIS_DIR), str(PROJECT_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)


PRESETS: Dict[str, List[str]] = {
    "A_current": [
        "--prob-calibration-mode", "enforce",
        "--stage1-shadow-empirical-mode", "shadow",
        "--stage1-alt-a-scope-mode", "enforce",
        "--under-emission-mode", "shadow",
        "--quote-engine-mode", "shadow",
        # 2026-06-10 fidelity re-sync: live runs gate_extreme_edge=0.30 via
        # cache/live_engine_overrides.json (2026-06-03 walk-forward RETUNE
        # promotion), but paper engines don't read the overrides file, so
        # A_current had silently drifted to the 0.22 signal_config default.
        # The baseline arm must mirror production or every X-vs-A paired
        # comparison carries a hidden confound. (gate_phantom_risk_band=0.70
        # needs no flag here -- DEFAULT_MAX_PHANTOM_RISK_SCORE is already
        # 0.70 in signal_config, matching the live override.)
        "--extreme-edge-max", "0.3",
    ],
    "B_cal_only": [
        "--prob-calibration-mode", "enforce",
        "--stage1-shadow-empirical-mode", "shadow",
        "--stage1-alt-a-scope-mode", "shadow",
        "--under-emission-mode", "shadow",
        "--quote-engine-mode", "shadow",
    ],
    "C_raw": [
        "--prob-calibration-mode", "shadow",
        "--stage1-shadow-empirical-mode", "shadow",
        "--stage1-alt-a-scope-mode", "shadow",
        "--under-emission-mode", "shadow",
        "--quote-engine-mode", "shadow",
    ],
    # 2026-05-25: D and E added to complete the scope x calibrator 2x2
    # factorial design and to test the edge-threshold lever. With A, B,
    # C, D, the 2x2 cleanly decomposes "scope effect with calibrator"
    # (A vs B) from "scope effect without calibrator" (D vs C). E tests
    # whether tightening min_edge by 5pp improves per-bet outcomes.
    "D_scope_only": [
        # Calibrator OFF (shadow), scope-enforce ON. Completes the 2x2.
        "--prob-calibration-mode", "shadow",
        "--stage1-shadow-empirical-mode", "shadow",
        "--stage1-alt-a-scope-mode", "enforce",
        "--under-emission-mode", "shadow",
        "--quote-engine-mode", "shadow",
    ],
    # E_tight_edge RETIRED 2026-06-10 (ran 2026-05-25 -> 2026-06-10,
    # 34 settled). CONCLUSION: tightening the edge floor +5pp LOSES
    # money -- the paired-delta vs A_current showed the 41 bets E
    # skipped won at 73.2% WR (+$32.71) while E's 10 unique bets went
    # 60% (-$5.39); net -$38 for tightening. Together with
    # G_loose_edge's mirror result (loosening -5pp also loses: the 40
    # added bets won only 65%, -$37), the fleet's verdict is that the
    # current 0.15 edge floor is locally optimal in both directions.
    # Matches the walk-forward cert's edge-band cohort table
    # (0.10-0.15 = -28.1% ROI; 0.15-0.22 = +14.2%). Question closed;
    # definition removed -- see git history to reproduce.
    # 2026-05-26: F-J added to expand the per-question coverage of the
    # parallel-engine fleet. Each maps to one open Active or Hygiene
    # roadmap item so the daily aggregate becomes the decision evidence.
    # Multi-engine has been the data-acceleration pivot since the 5-engine
    # debut on 2026-05-25; doubling to 10 ~doubles per-day candidates
    # without adding API or polling cost (shared market watcher).
    "F_no_dedup": [
        # Operator-proposed permissive config (2026-05-26): strip every
        # bet-suppression knob that's safe to strip in paper mode -- no
        # per-game cooldown, no inning gap, no correlated-line cap. Edge
        # and ask floors REMAIN; this isn't "anything goes," it's
        # "anything that passes the FV gates." Question: do dedup rules
        # leave money on the table by suppressing re-fires we'd want?
        "--prob-calibration-mode", "enforce",
        "--stage1-shadow-empirical-mode", "shadow",
        "--stage1-alt-a-scope-mode", "enforce",
        "--under-emission-mode", "shadow",
        "--quote-engine-mode", "shadow",
        "--event-dedup-secs", "0",
        "--inning-dedup-gap", "0",
        "--max-correlated-over-lines-per-game", "999",
        "--min-correlated-line-gap", "0.0",
    ],
    # G_loose_edge RETIRED 2026-06-10 (ran 2026-05-26 -> 2026-06-10,
    # 93 settled). CONCLUSION: loosening the edge floor -5pp LOSES
    # money -- paired-delta vs A_current: G's 40 unique (marginal-edge)
    # bets won only 65.0% (-$37.13), worse than the 11 it caused A-side
    # to miss. Confirms the 0.10-0.15 edge band is -EV (the 2026-05-25
    # audit's -28% ROI on n=14 was signal, not variance). Paired with
    # E_tight_edge's mirror result, the 0.15 floor is locally optimal.
    # Question closed; definition removed -- see git history.
    # H_late_innings RETIRED 2026-06-15 (ran 2026-05-26 -> 2026-06-15,
    # 18 shared days / 29 delta bets). Status at retirement: TRENDING
    # NEGATIVE but NOT statistically conclusive (paired-delta vs A_current
    # dNet -$52.24, Welch t=-0.65) -- so this is an operator-decision
    # retirement to reclaim fleet attention, NOT a concluded result like
    # E/G/N. Banning early innings (min-inning=6) removed good early bets
    # too, so the paper proxy net-lost vs A_current. The underlying
    # late-inning-tightening hypothesis stays OPEN and is tracked
    # canonically by the walk-forward cert's gate_high_line_min_inning
    # 5->6 RETUNE verdict on live filled bets -- the proper venue. Root
    # added to FLEET_RETIRED_ROOT_NAMES so the paired-delta block stops
    # emitting its verdict. Definition removed -- see git history.
    # I_extreme_018 RETIRED 2026-06-15 (18 shared days / 29 delta bets).
    # Status: TRENDING NEGATIVE, not conclusive (dNet -$21.84, t=-0.27).
    # The extreme_edge_max lever is owned canonically -- 0.22 -> 0.30 was
    # promoted live 2026-06-03 (walk-forward RETUNE), and J_no_phantom_filter
    # still supplies the edge>0.30 (cap-off) counterfactual cohort. A 0.18
    # (tighter-than-live) cap produced no distinct signal worth a dedicated
    # arm. Retired by operator decision to reclaim a slot for the
    # gate_max_base_fv decision arm (Q). Root added to
    # FLEET_RETIRED_ROOT_NAMES. Definition removed -- see git history.
    "J_no_phantom_filter": [
        # A_current but extreme_edge_max effectively disabled (1.0). The
        # other side of the I_extreme_018 / TR19-tightening sweep. The
        # 2026-04-28 -> 2026-05-03 window analysis that motivated TR19
        # showed edge>0.20 cohort was 4W/8L at -$79.57 on 12 bets, but
        # that was pre-TR20. Need fresh evidence to confirm extreme-edge
        # is STILL the loser cohort under v2 Stage-3. If J flips
        # profitable in 30 days, TR19 itself is up for re-evaluation.
        "--prob-calibration-mode", "enforce",
        "--stage1-shadow-empirical-mode", "shadow",
        "--stage1-alt-a-scope-mode", "enforce",
        "--under-emission-mode", "shadow",
        "--quote-engine-mode", "shadow",
        "--extreme-edge-max", "1.0",
    ],
    # 2026-05-26: K_line5p5_block ships Hygiene #1 as a paper preset
    # for A/B evidence vs A_current. The 2026-05-19 FV-overconfidence
    # audit found line=5.5 at base FV >= 0.90 hits 51% realized vs ~96%
    # claimed on n=92 -- the worst per-line slice in the dataset. K
    # enforces a per-line guard that blocks these bets entirely; A
    # continues to take them. After ~30 days the operator compares
    # K vs A on (n_settled, profit, profit_per_settled_bet) to decide
    # whether to promote the guard to live.
    "K_line5p5_block": [
        "--prob-calibration-mode", "enforce",
        "--stage1-shadow-empirical-mode", "shadow",
        "--stage1-alt-a-scope-mode", "enforce",
        "--under-emission-mode", "shadow",
        "--quote-engine-mode", "shadow",
        "--line-high-fv-block-mode", "enforce",
        "--line-high-fv-block-min-raw-fv", "0.90",
        "--line-high-fv-block-lines", "5.5",
    ],
    # 2026-05-28: L_enforce_min_raw_095 A/B-tests the calibration
    # edge-shaving deep dive (analyze_calibration_edge_shaving.py). That
    # report found the band-gated calibrator flattens a realized +EV
    # high-FV sub-band ([0.90,0.95): WR 0.796 vs ask 0.744) together with
    # the -EV overconfident tail ([0.95,1.0): WR 0.689 vs ask 0.824), and
    # recommended raising enforce_min_raw 0.90 -> 0.95 so the +EV band keeps
    # its raw FV while the tail stays shrunk. L = A_current with the higher
    # band gate; A_current keeps 0.90. After ~30 days the operator compares
    # L vs A on (n_settled, profit, profit_per_settled_bet, filled WR) to
    # decide whether to flip DEFAULT_PROB_CALIBRATION_ENFORCE_MIN_RAW live.
    "L_enforce_min_raw_095": [
        "--prob-calibration-mode", "enforce",
        "--stage1-shadow-empirical-mode", "shadow",
        "--stage1-alt-a-scope-mode", "enforce",
        "--under-emission-mode", "shadow",
        "--quote-engine-mode", "shadow",
        "--prob-calibration-enforce-min-raw", "0.95",
    ],
    # 2026-05-28: M_under_paper is the no-risk paper mirror of live UNDER
    # trading. A_current baseline + `--under-mode paper`, so when the live
    # engine runs `--under-mode live` for real-money fill data, this paper
    # config accumulates the matching paper-UNDER bets that feed the B4
    # 60-session validation milestone (sessions / n_settled / ROI /
    # calibration / drift) WITHOUT real-money risk. It is the research
    # counterpart to the live UNDER posture.
    "M_under_paper": [
        "--prob-calibration-mode", "enforce",
        "--stage1-shadow-empirical-mode", "shadow",
        "--stage1-alt-a-scope-mode", "enforce",
        "--quote-engine-mode", "shadow",
        "--under-mode", "paper",
        # 2026-06-11: calibration flipped back ON (was off since
        # 2026-05-30). The off-mode stop-gap produced honest volume but
        # dishonest FVs: 8 UNDER bets at raw 1-over_fv (overconfident
        # by construction), 1W/7L, -69% ROI, calibration delta -39pp.
        # B4 can never clear on that data. The 2026-06-11 evaluation
        # found the pooled UNDER calibrator scores logloss 0.711 on
        # held-out rows (vs 0.813 for a per-line variant, which is
        # OVERFIT on current n -- line-5.5 isotonic maps raw 0.05 to
        # 0.82; per-line UNDER is deferred until real UNDER outcomes
        # accumulate). The score_event curve remains low-discrimination
        # (flat ~0.30) so enforce mode fires only cheap-ask unders;
        # fewer bets, but ones whose FV is defensible -- which is what
        # the B4 ROI + calibration conditions actually need.
        "--under-calibration-mode", "enforce",
    ],
    # N_extreme_edge_022 RETIRED 2026-06-10 (ran 2026-06-01 -> 2026-06-10,
    # 47 settled). NULL EXPERIMENT: the preset's premise ("production
    # runs --extreme-edge-max 1.0, disabled") was wrong -- A_current was
    # running the signal_config default of 0.22, identical to N. Result:
    # 10 days, 47 settled bets, ZERO delta decisions vs A_current
    # (paired analysis found 0 unique bets either way). The 0.22-vs-0.30
    # question is owned by the 2026-06-03 live promotion + the armed
    # fast Wilson-UB demote check; J_no_phantom_filter (cap=1.0)
    # continues to provide the edge>0.30 counterfactual cohort.
    # Definition removed -- see git history.
    #
    # 2026-06-11: O + P are the first MODEL-VERSION arms (the fleet
    # audit's core recommendation: test model versions, not just gate
    # variants). Both are A_current + a Stage-1 cache swap, calibrator
    # ON so the RF2 interaction (redundant correction between FV-level
    # fixes and the calibrator) is measured, not assumed. The paired-
    # delta daily-review block reads both vs A_current automatically.
    # Caches are staging artifacts the daily refresh keeps fresh; they
    # are NEVER auto-promoted (promote.py stage1 is the manual gate).
    "O_nb_stage1": [
        # Hygiene #3: negative-binomial Stage-1 tail. Same data window
        # as production; only the smoothing differs (per-phase NB
        # dispersion via method of moments; non-overdispersed phases
        # keep Poisson). Tests the structural fix for the chronic
        # +18pp raw-FV bias (4-7pp poisson>empirical at FV>=0.85).
        "--prob-calibration-mode", "enforce",
        "--stage1-shadow-empirical-mode", "shadow",
        "--stage1-alt-a-scope-mode", "enforce",
        "--under-emission-mode", "shadow",
        "--quote-engine-mode", "shadow",
        "--extreme-edge-max", "0.3",
        "--cache-path", "cache/mlb_ou_cache_nb.staging.json",
    ],
    "P_alt_a_cache": [
        # Active #8: the FULL Alt-A cache (empirical_when_available) as
        # Stage-1 -- the exact artifact promote.py stage1 would swap in.
        # Live-fire evidence for the promotion candidate; pairs with
        # O so the RF2 decision (~06-17) compares both Stage-1 fixes
        # under the production calibrator.
        "--prob-calibration-mode", "enforce",
        "--stage1-shadow-empirical-mode", "shadow",
        "--stage1-alt-a-scope-mode", "enforce",
        "--under-emission-mode", "shadow",
        "--quote-engine-mode", "shadow",
        "--extreme-edge-max", "0.3",
        "--cache-path", "cache/mlb_ou_cache_alt_a.staging.json",
    ],
    "Q_max_base_fv_095": [
        # T3 (2026-06-15) decision arm. A_current EXACTLY + gate_max_base_fv
        # tightened 0.99 -> 0.95, isolating that one lever so the
        # paired-delta block accrues FV-level evidence on the 2026-06-14
        # audit's recommendation (gate_counterfactual: +$73/30d, +$152
        # lifetime, no window-reversal; the cert's own sweep agrees -- kept
        # ROI +4.85% -> +11.6%, the 0.95-0.99 band is 75 filled bets @
        # -15.8%; the cert's nominal KEEP is a verdict-heuristic artifact
        # that only inspects the current threshold). Goal: turn the held
        # sign-off into a data-backed flip on live re-entry instead of a
        # cold one. Mirrors A_current's flags so the ONLY delta is the cap.
        "--prob-calibration-mode", "enforce",
        "--stage1-shadow-empirical-mode", "shadow",
        "--stage1-alt-a-scope-mode", "enforce",
        "--under-emission-mode", "shadow",
        "--quote-engine-mode", "shadow",
        "--extreme-edge-max", "0.3",
        "--max-base-fv", "0.95",
    ],
}

PRESET_ALIASES = {
    "enforce_enforce": "A_current",
    "enforce_shadow": "B_cal_only",
    "shadow_shadow": "C_raw",
}

RESERVED_ENGINE_FLAGS = {
    "--paper-root",
    "--config-label",
    "--startup-refresh",
    "--no-startup-refresh",
    "--require-fresh-refresh",
}

LIVE_ONLY_ENGINE_FLAGS = {
    "--ask-reversal-drop",
    "--ask-reversal-window",
    "--calibrated-stake-scale-mode",
    "--daily-budget",
    "--fv-cancel-min-edge",
    "--fv-decay-min-age-secs",
    "--fv-decay-min-ask-drop",
    "--kelly-floor-to-min",
    "--kelly-fraction",
    "--kelly-max-bet-fraction",
    "--kelly-max-edge",
    "--max-open-orders",
    "--min-order-size",
    "--order-timeout-secs",
    "--per-game-budget-fraction",
    "--spread-factor",
    "--stake-mode",
    "--wait-for-clob",
}

LABEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


@dataclass(frozen=True)
class EngineConfig:
    label: str
    flags: List[str]
    source: str
    paper_root: Path


@dataclass
class RunningEngine:
    config: EngineConfig
    process: subprocess.Popen
    log_path: Path
    start_time: float
    reported_exit: bool = False


def _default_date(timezone: str) -> str:
    if ZoneInfo is not None:
        try:
            return datetime.now(ZoneInfo(timezone)).strftime("%Y-%m-%d")
        except Exception:
            pass
    return datetime.now().strftime("%Y-%m-%d")


def _validate_label(label: str) -> str:
    label = str(label or "").strip()
    if not LABEL_RE.match(label):
        raise SystemExit(
            f"Bad config label '{label}'. Use letters, numbers, dot, dash, "
            "or underscore; max 64 chars."
        )
    if label.lower() == "trading":
        raise SystemExit(
            "Refusing config label 'trading': with the default prefix this "
            "would collide with data/paper_trading."
        )
    return label


def _split_custom_flags(spec: str) -> List[str]:
    raw = spec.replace(",", " ").strip()
    if not raw:
        return []
    return raw.split()


def _resolve_config(raw: str, prefix: Path) -> EngineConfig:
    raw = str(raw or "").strip()
    if not raw:
        raise SystemExit("--config cannot be empty")

    if ":" in raw:
        label_part, spec_part = raw.split(":", 1)
        label = _validate_label(label_part)
        preset_name = PRESET_ALIASES.get(spec_part, spec_part)
        if preset_name in PRESETS:
            flags = list(PRESETS[preset_name])
            source = preset_name
        else:
            flags = _split_custom_flags(spec_part)
            source = "custom"
    else:
        preset_name = PRESET_ALIASES.get(raw, raw)
        if preset_name not in PRESETS:
            raise SystemExit(
                f"Unknown config '{raw}'. Use one of: "
                f"{', '.join(sorted(PRESETS))}, or label:preset."
            )
        label = _validate_label(preset_name)
        flags = list(PRESETS[preset_name])
        source = preset_name

    return EngineConfig(
        label=label,
        flags=flags,
        source=source,
        paper_root=Path(f"{prefix}{label}"),
    )


def _has_reserved_unknown_flags(args: Sequence[str]) -> Optional[str]:
    for token in args:
        key = token.split("=", 1)[0]
        if key in RESERVED_ENGINE_FLAGS:
            return key
    return None


def _live_only_unknown_flags(args: Sequence[str]) -> List[str]:
    found: List[str] = []
    for token in args:
        key = token.split("=", 1)[0]
        if key in LIVE_ONLY_ENGINE_FLAGS and key not in found:
            found.append(key)
    return found


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Launch parallel paper SignalEngine configs.")
    p.add_argument(
        "--config",
        action="append",
        default=[],
        help=(
            "Config preset or label:preset. Presets: A_current, "
            "B_cal_only, C_raw, D_scope_only, F_no_dedup, "
            "H_late_innings, I_extreme_018, J_no_phantom_filter, "
            "K_line5p5_block, L_enforce_min_raw_095, M_under_paper, "
            "O_nb_stage1, P_alt_a_cache. "
            "Aliases: enforce_enforce, enforce_shadow, shadow_shadow. "
            "May be repeated. Default (no --config): all 13 presets."
        ),
    )
    p.add_argument(
        "--paper-root-prefix",
        type=Path,
        default=PROJECT_DIR / "data" / "paper_",
        help="Prefix used to form each paper root as <prefix><label>.",
    )
    p.add_argument("--stake", type=float, default=10.0, help="Paper stake passed to each engine.")
    p.add_argument("--timezone", type=str, default="America/Toronto")
    p.add_argument("--date", type=str, default="", help="Optional active date YYYY-MM-DD.")
    p.add_argument("--capture-duration", type=float, default=None)
    p.add_argument("--capture-interval", type=float, default=None)
    p.add_argument("--capture-depth", type=int, default=None)
    p.add_argument("--log-level", type=str, default="")
    p.add_argument(
        "--market-data-mode",
        choices=["shared", "per-engine"],
        default="shared",
        help=(
            "shared = one watcher polls MLB/Polymarket and feeds all paper "
            "engines (default). per-engine = legacy behavior where each "
            "paper_trader.py process runs its own monitor."
        ),
    )
    p.add_argument(
        "--shared-output-root",
        type=Path,
        default=DEFAULT_SHARED_OUTPUT_ROOT,
        help="Raw monitor output root used by the shared watcher.",
    )
    p.add_argument("--consumer-startup-timeout-secs", type=float, default=45.0)
    p.add_argument("--bus-max-queue-batches", type=int, default=120)
    p.add_argument("--bus-warn-queue-batches", type=int, default=50)
    p.add_argument(
        "--shared-capture-mode",
        choices=["on", "off"],
        default="on",
        help="In shared market-data mode, centralize depth/tape/post-signal captures in the watcher (default: on).",
    )
    p.add_argument("--shared-capture-depth-bucket-ms", type=int, default=1000)
    p.add_argument("--shared-capture-tape-ttl-secs", type=float, default=5.0)
    p.add_argument("--shared-capture-signal-bucket-ms", type=int, default=1000)
    p.add_argument("--shared-capture-timeout-secs", type=float, default=8.0)
    p.add_argument("--poll-interval", type=float, default=None)
    p.add_argument("--schedule-refresh-secs", type=float, default=None)
    p.add_argument("--discovery-refresh-secs", type=float, default=None)
    p.add_argument("--inter-request-delay", type=float, default=None)
    p.add_argument("--max-workers", type=int, default=None)
    p.add_argument("--gamma-timeout", type=float, default=None)
    p.add_argument("--clob-timeout", type=float, default=None)
    p.add_argument("--start-on-preview", action="store_true", default=False)
    p.add_argument("--once", action="store_true", default=False)
    p.add_argument("--run-seconds", type=int, default=None)
    p.add_argument("--book-failure-retire-streak", type=int, default=None)
    p.add_argument("--book-failure-cooldown-secs", type=float, default=None)
    p.add_argument("--book-failure-max-cooldown-secs", type=float, default=None)
    p.add_argument("--pitcher-cache-path", type=str, default="")
    p.add_argument("--performance-mode", dest="performance_mode", action="store_true", default=None)
    p.add_argument("--no-performance-mode", dest="performance_mode", action="store_false")
    p.add_argument("--p-core-affinity", type=str, default="")
    p.add_argument(
        "--startup-refresh",
        dest="startup_refresh",
        action="store_true",
        default=True,
        help="Run paper startup refresh once before launching children (default).",
    )
    p.add_argument(
        "--no-startup-refresh",
        dest="startup_refresh",
        action="store_false",
        help="Skip the launcher-level startup refresh.",
    )
    p.add_argument("--startup-refresh-strict", action="store_true", default=False)
    p.add_argument("--require-fresh-refresh", action="store_true", default=False)
    p.add_argument("--max-refresh-age-hours", type=float, default=60.0)
    p.add_argument(
        "--python",
        default=sys.executable,
        help=f"Python executable for child engines (default: {sys.executable}).",
    )
    p.add_argument(
        "--engine-script",
        type=Path,
        default=PROJECT_DIR / "scripts" / "trading" / "paper_trader.py",
        help="Paper engine entrypoint.",
    )
    p.add_argument(
        "--consumer-script",
        type=Path,
        default=PROJECT_DIR / "scripts" / "trading" / "paper_engine_consumer.py",
        help="Shared market-data paper consumer entrypoint.",
    )
    p.add_argument(
        "--watcher-script",
        type=Path,
        default=PROJECT_DIR / "scripts" / "trading" / "shared_market_watcher.py",
        help="Shared market-data watcher entrypoint.",
    )
    p.add_argument(
        "--dry-launch",
        action="store_true",
        help="Print child commands and exit without starting processes.",
    )
    p.add_argument(
        "--startup-health-secs",
        type=float,
        default=30.0,
        help=(
            "Seconds after launch to treat a non-zero child exit as an "
            "early startup failure and print that engine's log tail."
        ),
    )
    # Post-session aggregator hook (2026-05-26): when the launcher exits
    # after all engines stop, run aggregate_parallel_engines.py for the
    # active date so the canonical
    # data/analysis_output/parallel_engine_comparison/ report reflects
    # what just happened. Fail-open: aggregator errors do not change the
    # launcher's exit code. Disable for debugging / dry runs with
    # --no-post-session-aggregate.
    p.add_argument(
        "--post-session-aggregate",
        dest="post_session_aggregate",
        action="store_true",
        default=True,
        help=(
            "After all engines exit, run aggregate_parallel_engines.py "
            "for the active date (default: on)."
        ),
    )
    p.add_argument(
        "--no-post-session-aggregate",
        dest="post_session_aggregate",
        action="store_false",
        help="Skip the end-of-session aggregator run.",
    )
    p.add_argument(
        "--post-session-aggregate-script",
        type=Path,
        default=PROJECT_DIR / "scripts" / "analysis" / "aggregate_parallel_engines.py",
        help="Path to aggregate_parallel_engines.py.",
    )
    args, unknown = p.parse_known_args(argv)
    reserved = _has_reserved_unknown_flags(unknown)
    if reserved:
        raise SystemExit(
            f"{reserved} is owned by launch_parallel_engines.py; do not pass it "
            "through as an engine override."
        )
    live_only = _live_only_unknown_flags(unknown)
    if live_only:
        raise SystemExit(
            "launch_parallel_engines.py runs paper_trader.py children, but "
            "these live-execution-only flags were supplied and paper_trader.py "
            f"will reject them: {', '.join(live_only)}. Remove them for the "
            "parallel paper experiment; keep shared signal/monitor flags such "
            "as --stake, --timezone, --capture-duration, --performance-mode, "
            "and --pitcher-cache-path."
        )
    args.engine_overrides = list(unknown)
    return args


def _common_trade_flags(args: argparse.Namespace) -> List[str]:
    flags = [
        "--stake", str(args.stake),
    ]
    if args.capture_duration is not None:
        flags.extend(["--capture-duration", str(args.capture_duration)])
    if args.capture_interval is not None:
        flags.extend(["--capture-interval", str(args.capture_interval)])
    if args.capture_depth is not None:
        flags.extend(["--capture-depth", str(args.capture_depth)])
    return flags


def _common_monitor_flags(args: argparse.Namespace, *, output_root: Optional[Path] = None) -> List[str]:
    flags = ["--timezone", str(args.timezone)]
    if args.date:
        flags.extend(["--date", str(args.date)])
    if output_root is not None:
        flags.extend(["--output-root", str(output_root)])
    if args.log_level:
        flags.extend(["--log-level", str(args.log_level)])
    for attr, flag in [
        ("poll_interval", "--poll-interval"),
        ("schedule_refresh_secs", "--schedule-refresh-secs"),
        ("discovery_refresh_secs", "--discovery-refresh-secs"),
        ("inter_request_delay", "--inter-request-delay"),
        ("max_workers", "--max-workers"),
        ("gamma_timeout", "--gamma-timeout"),
        ("clob_timeout", "--clob-timeout"),
        ("run_seconds", "--run-seconds"),
        ("book_failure_retire_streak", "--book-failure-retire-streak"),
        ("book_failure_cooldown_secs", "--book-failure-cooldown-secs"),
        ("book_failure_max_cooldown_secs", "--book-failure-max-cooldown-secs"),
    ]:
        value = getattr(args, attr, None)
        if value is not None:
            flags.extend([flag, str(value)])
    if args.start_on_preview:
        flags.append("--start-on-preview")
    if args.once:
        flags.append("--once")
    if args.pitcher_cache_path:
        flags.extend(["--pitcher-cache-path", str(args.pitcher_cache_path)])
    if args.performance_mode is True:
        flags.append("--performance-mode")
    elif args.performance_mode is False:
        flags.append("--no-performance-mode")
    if args.p_core_affinity:
        flags.extend(["--p-core-affinity", str(args.p_core_affinity)])
    return flags


def _common_engine_flags(args: argparse.Namespace) -> List[str]:
    return [*_common_trade_flags(args), *_common_monitor_flags(args)]


def build_engine_command(
    args: argparse.Namespace,
    cfg: EngineConfig,
) -> List[str]:
    cmd = [
        str(args.python),
        str(args.engine_script),
        *cfg.flags,
        *_common_engine_flags(args),
        *args.engine_overrides,
        "--paper-root", str(cfg.paper_root),
        "--config-label", cfg.label,
        "--no-startup-refresh",
    ]
    return cmd


def build_consumer_command(
    args: argparse.Namespace,
    cfg: EngineConfig,
    *,
    bus_host: str,
    bus_port: int,
    bus_authkey: str,
    capture_bus_host: str = "",
    capture_bus_port: int = 0,
    capture_bus_authkey: str = "",
    watcher_pid: int = 0,
) -> List[str]:
    cmd = [
        str(args.python),
        str(args.consumer_script),
        *cfg.flags,
        *_common_trade_flags(args),
        *_common_monitor_flags(args),
        *args.engine_overrides,
        "--paper-root", str(cfg.paper_root),
        "--config-label", cfg.label,
        "--no-startup-refresh",
        "--bus-host", str(bus_host),
        "--bus-port", str(bus_port),
        "--bus-authkey", str(bus_authkey),
        "--watcher-pid", str(watcher_pid),
        "--consumer-connect-timeout-secs", str(args.consumer_startup_timeout_secs),
    ]
    if capture_bus_port > 0 and capture_bus_authkey:
        cmd.extend(
            [
                "--capture-bus-host", str(capture_bus_host or bus_host),
                "--capture-bus-port", str(capture_bus_port),
                "--capture-bus-authkey", str(capture_bus_authkey),
                "--shared-capture-timeout-secs", str(args.shared_capture_timeout_secs),
            ]
        )
    return cmd


def build_watcher_command(
    args: argparse.Namespace,
    *,
    bus_host: str,
    bus_port: int,
    bus_authkey: str,
    ready_file: Path,
    expected_consumers: int,
    capture_bus_host: str = "",
    capture_bus_port: int = 0,
    capture_bus_authkey: str = "",
) -> List[str]:
    cmd = [
        str(args.python),
        str(args.watcher_script),
        "--bus-host", str(bus_host),
        "--bus-port", str(bus_port),
        "--bus-authkey", str(bus_authkey),
        "--ready-file", str(ready_file),
        "--bus-max-queue-batches", str(args.bus_max_queue_batches),
        "--bus-warn-queue-batches", str(args.bus_warn_queue_batches),
        "--expected-consumers", str(expected_consumers),
        "--consumer-wait-timeout-secs", str(args.consumer_startup_timeout_secs),
        *_common_monitor_flags(args, output_root=args.shared_output_root),
    ]
    if capture_bus_port > 0 and capture_bus_authkey:
        cmd.extend(
            [
                "--capture-bus-host", str(capture_bus_host or bus_host),
                "--capture-bus-port", str(capture_bus_port),
                "--capture-bus-authkey", str(capture_bus_authkey),
                "--shared-capture-root", str(args.shared_output_root),
                "--shared-capture-depth-bucket-ms", str(args.shared_capture_depth_bucket_ms),
                "--shared-capture-tape-ttl-secs", str(args.shared_capture_tape_ttl_secs),
                "--shared-capture-signal-bucket-ms", str(args.shared_capture_signal_bucket_ms),
            ]
        )
    return cmd


def _pick_free_port(host: str = "127.0.0.1") -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((host, 0))
        return int(sock.getsockname()[1])


def _run_refresh_once(args: argparse.Namespace, active_date: str) -> None:
    if not args.startup_refresh:
        print("Launcher startup refresh disabled by --no-startup-refresh.")
        return
    from signal_engine import (
        _check_refresh_freshness,
        _run_paper_startup_refresh,
    )

    trade_args = SimpleNamespace(
        startup_refresh=True,
        startup_refresh_strict=bool(args.startup_refresh_strict),
        stake=float(args.stake),
    )
    print(f"Running paper startup refresh once for active_date={active_date}...")
    _run_paper_startup_refresh(date_str=active_date, trade_args=trade_args)

    if args.require_fresh_refresh:
        snap = _check_refresh_freshness(
            active_date,
            alert_hours=float(args.max_refresh_age_hours),
        )
        if snap.get("status") == "alert":
            raise SystemExit(
                "Aborting: --require-fresh-refresh set and latest startup "
                f"refresh is stale: {snap}"
            )


def _env_for_child() -> Dict[str, str]:
    env = dict(os.environ)
    path_parts = [
        str(TRADING_DIR),
        str(MONITOR_DIR),
        str(ANALYSIS_DIR),
        str(PROJECT_DIR),
    ]
    existing = env.get("PYTHONPATH")
    if existing:
        path_parts.append(existing)
    env["PYTHONPATH"] = os.pathsep.join(path_parts)
    return env


def _terminate_all(processes: Iterable[subprocess.Popen]) -> None:
    alive = [p for p in processes if p.poll() is None]
    if not alive:
        return
    print(f"Terminating {len(alive)} engine process(es)...")
    for proc in alive:
        try:
            proc.terminate()
        except Exception:
            pass
    deadline = time.time() + 30.0
    while time.time() < deadline:
        if all(p.poll() is not None for p in alive):
            return
        time.sleep(0.5)
    for proc in alive:
        if proc.poll() is None:
            try:
                proc.kill()
            except Exception:
                pass


def _tail_file(path: Path, *, max_lines: int = 40) -> List[str]:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    return lines[-max_lines:]


def _print_log_tail(label: str, path: Path) -> None:
    tail = _tail_file(path)
    if not tail:
        print(f"No log tail available for {label} at {path}")
        return
    print(f"--- {label} log tail ({path}) ---")
    for line in tail:
        print(line)
    print(f"--- end {label} log tail ---")


def _wait_for_engines(
    running: Sequence[RunningEngine],
    *,
    startup_health_secs: float,
) -> int:
    exit_code = 0
    remaining = len(running)
    while remaining > 0:
        for engine in running:
            if engine.reported_exit:
                continue
            rc = engine.process.poll()
            if rc is None:
                continue
            engine.reported_exit = True
            remaining -= 1
            runtime = time.time() - engine.start_time
            print(f"Engine {engine.config.label} exited with code {rc} after {runtime:.1f}s")
            if rc != 0 and exit_code == 0:
                exit_code = int(rc)
            if rc != 0 and runtime <= max(0.0, startup_health_secs):
                print(
                    f"Early startup failure detected for {engine.config.label} "
                    f"within {startup_health_secs:.1f}s."
                )
                _print_log_tail(engine.config.label, engine.log_path)
        if remaining > 0:
            time.sleep(1.0)
    return exit_code


def _wait_for_ready_file(path: Path, proc: subprocess.Popen, timeout_secs: float) -> Dict[str, object]:
    deadline = time.time() + max(1.0, float(timeout_secs))
    while time.time() < deadline:
        if path.exists():
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                return {"ready": True, "path": str(path)}
        rc = proc.poll()
        if rc is not None:
            raise SystemExit(f"Shared watcher exited before ready file appeared (code={rc}).")
        time.sleep(0.25)
    raise SystemExit(f"Timed out waiting for shared watcher ready file: {path}")


def _open_launch_log(path: Path, command: Sequence[str]):
    path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(path, "a", encoding="utf-8")
    fh.write("\n=== launch_parallel_engines start ===\n")
    fh.write("command: " + " ".join(command) + "\n")
    fh.flush()
    return fh


def _run_per_engine_mode(
    args: argparse.Namespace,
    configs: Sequence[EngineConfig],
    commands: Sequence[Tuple[EngineConfig, List[str]]],
    active_date: str,
) -> int:
    print(
        f"Parallel paper launch: {len(configs)} config(s), active_date={active_date}, "
        "market_data_mode=per-engine. Expect roughly "
        f"{len(configs)}x normal paper API usage and disk usage."
    )
    for cfg, cmd in commands:
        print(f"  {cfg.label} ({cfg.source}) -> {cfg.paper_root}")
        print("    " + " ".join(cmd))
    if args.dry_launch:
        return 0

    _run_refresh_once(args, active_date)

    env = _env_for_child()
    processes: List[subprocess.Popen] = []
    running: List[RunningEngine] = []
    log_handles = []

    def _signal_handler(signum, _frame) -> None:  # type: ignore[no-untyped-def]
        print(f"Received signal {signum}; shutting down child engines...")
        _terminate_all(processes)

    old_int = signal.signal(signal.SIGINT, _signal_handler)
    old_term = signal.signal(signal.SIGTERM, _signal_handler)
    try:
        for cfg, cmd in commands:
            log_path = cfg.paper_root / "launch_log.txt"
            fh = _open_launch_log(log_path, cmd)
            log_handles.append(fh)
            proc = subprocess.Popen(
                cmd,
                cwd=str(PROJECT_DIR),
                stdout=fh,
                stderr=subprocess.STDOUT,
                env=env,
            )
            processes.append(proc)
            running.append(RunningEngine(cfg, proc, log_path, time.time()))
            print(f"Started {cfg.label}: pid={proc.pid} log={log_path}")
        return _wait_for_engines(running, startup_health_secs=float(args.startup_health_secs))
    finally:
        signal.signal(signal.SIGINT, old_int)
        signal.signal(signal.SIGTERM, old_term)
        _terminate_all(processes)
        for fh in log_handles:
            try:
                fh.close()
            except Exception:
                pass


def _run_shared_mode(
    args: argparse.Namespace,
    configs: Sequence[EngineConfig],
    active_date: str,
) -> int:
    bus_host = "127.0.0.1"
    bus_port = _pick_free_port(bus_host)
    bus_authkey = secrets.token_hex(16)
    capture_enabled = str(args.shared_capture_mode) == "on"
    capture_bus_host = bus_host
    capture_bus_port = _pick_free_port(capture_bus_host) if capture_enabled else 0
    if capture_enabled:
        while capture_bus_port == bus_port:
            capture_bus_port = _pick_free_port(capture_bus_host)
    capture_bus_authkey = secrets.token_hex(16) if capture_enabled else ""
    ready_dir = PROJECT_DIR / "data" / "analysis_output" / "parallel_engine_comparison"
    ready_file = ready_dir / f"shared_watcher_ready_{os.getpid()}_{int(time.time())}.json"
    watcher_cmd = build_watcher_command(
        args,
        bus_host=bus_host,
        bus_port=bus_port,
        bus_authkey=bus_authkey,
        ready_file=ready_file,
        expected_consumers=len(configs),
        capture_bus_host=capture_bus_host,
        capture_bus_port=capture_bus_port,
        capture_bus_authkey=capture_bus_authkey,
    )
    dry_consumer_cmds = [
        (
            cfg,
            build_consumer_command(
                args,
                cfg,
                bus_host=bus_host,
                bus_port=bus_port,
                bus_authkey=bus_authkey,
                capture_bus_host=capture_bus_host,
                capture_bus_port=capture_bus_port,
                capture_bus_authkey=capture_bus_authkey,
                watcher_pid=0,
            ),
        )
        for cfg in configs
    ]
    print(
        f"Parallel paper launch: {len(configs)} config(s), active_date={active_date}, "
        "market_data_mode=shared. One watcher will poll MLB/Polymarket; "
        f"expect roughly 1x API usage and {len(configs)}x paper output."
    )
    print(f"  shared_watcher -> {args.shared_output_root}")
    print("    " + " ".join(watcher_cmd))
    for cfg, cmd in dry_consumer_cmds:
        print(f"  {cfg.label} ({cfg.source}) -> {cfg.paper_root}")
        print("    " + " ".join(cmd))
    if args.dry_launch:
        return 0

    _run_refresh_once(args, active_date)

    env = _env_for_child()
    processes: List[subprocess.Popen] = []
    consumer_processes: List[subprocess.Popen] = []
    running: List[RunningEngine] = []
    log_handles = []
    watcher_proc: Optional[subprocess.Popen] = None
    watcher_log = args.shared_output_root / "shared_watcher_launch_log.txt"

    def _signal_handler(signum, _frame) -> None:  # type: ignore[no-untyped-def]
        print(f"Received signal {signum}; shutting down shared watcher and child engines...")
        _terminate_all(processes)

    old_int = signal.signal(signal.SIGINT, _signal_handler)
    old_term = signal.signal(signal.SIGTERM, _signal_handler)
    try:
        watcher_fh = _open_launch_log(watcher_log, watcher_cmd)
        log_handles.append(watcher_fh)
        watcher_proc = subprocess.Popen(
            watcher_cmd,
            cwd=str(PROJECT_DIR),
            stdout=watcher_fh,
            stderr=subprocess.STDOUT,
            env=env,
        )
        processes.append(watcher_proc)
        print(f"Started shared watcher: pid={watcher_proc.pid} log={watcher_log}")
        ready = _wait_for_ready_file(
            ready_file,
            watcher_proc,
            timeout_secs=float(args.consumer_startup_timeout_secs),
        )
        print(f"Shared watcher ready: {ready}")

        for cfg in configs:
            cmd = build_consumer_command(
                args,
                cfg,
                bus_host=bus_host,
                bus_port=bus_port,
                bus_authkey=bus_authkey,
                capture_bus_host=capture_bus_host,
                capture_bus_port=capture_bus_port,
                capture_bus_authkey=capture_bus_authkey,
                watcher_pid=int(watcher_proc.pid or 0),
            )
            log_path = cfg.paper_root / "launch_log.txt"
            fh = _open_launch_log(log_path, cmd)
            log_handles.append(fh)
            proc = subprocess.Popen(
                cmd,
                cwd=str(PROJECT_DIR),
                stdout=fh,
                stderr=subprocess.STDOUT,
                env=env,
            )
            processes.append(proc)
            consumer_processes.append(proc)
            running.append(RunningEngine(cfg, proc, log_path, time.time()))
            print(f"Started {cfg.label}: pid={proc.pid} log={log_path}")

        exit_code = 0
        remaining = len(running)
        watcher_reported = False
        while remaining > 0:
            watcher_rc = watcher_proc.poll()
            if watcher_rc is not None and not watcher_reported:
                watcher_reported = True
                print(f"Shared watcher exited with code {watcher_rc}")
                if watcher_rc != 0 and exit_code == 0:
                    exit_code = int(watcher_rc)
                deadline = time.time() + 10.0
                while time.time() < deadline and any(p.poll() is None for p in consumer_processes):
                    time.sleep(0.5)
                _terminate_all(consumer_processes)
            for engine in running:
                if engine.reported_exit:
                    continue
                rc = engine.process.poll()
                if rc is None:
                    continue
                engine.reported_exit = True
                remaining -= 1
                runtime = time.time() - engine.start_time
                print(f"Engine {engine.config.label} exited with code {rc} after {runtime:.1f}s")
                if rc != 0 and exit_code == 0:
                    exit_code = int(rc)
                if rc != 0 and runtime <= max(0.0, float(args.startup_health_secs)):
                    print(
                        f"Early startup failure detected for {engine.config.label} "
                        f"within {float(args.startup_health_secs):.1f}s."
                    )
                    _print_log_tail(engine.config.label, engine.log_path)
            if remaining > 0:
                time.sleep(1.0)
        watcher_rc = watcher_proc.poll()
        if watcher_rc is None:
            _terminate_all([watcher_proc])
        elif watcher_rc != 0 and exit_code == 0:
            exit_code = int(watcher_rc)
        return exit_code
    finally:
        signal.signal(signal.SIGINT, old_int)
        signal.signal(signal.SIGTERM, old_term)
        _terminate_all(processes)
        for fh in log_handles:
            try:
                fh.close()
            except Exception:
                pass


def _run_post_session_aggregator(
    args: argparse.Namespace,
    configs: Sequence[EngineConfig],
    active_date: str,
) -> None:
    """Run aggregate_parallel_engines.py for `active_date` once all engines
    have exited. Fail-open: any error is printed and swallowed so the
    launcher's exit code is unchanged.

    Audit motivation (2026-05-26): yesterday's 2026-05-25 multi-engine run
    finished cleanly but the canonical
    `data/analysis_output/parallel_engine_comparison/` report kept showing
    2026-05-24 data because nobody re-ran the aggregator after the session.
    The fix lives in the launcher (rather than run_daily_refresh) so the
    aggregator runs RIGHT WHEN the data lands, not at next-morning refresh
    time."""
    if not args.post_session_aggregate or args.dry_launch:
        return
    script = Path(args.post_session_aggregate_script)
    if not script.exists():
        print(
            f"[post-session-aggregator] skipped: script not found at {script}"
        )
        return

    paper_roots = ",".join(str(cfg.paper_root) for cfg in configs)
    cmd = [
        sys.executable or "python",
        str(script),
        "--paper-roots", paper_roots,
        "--date-range", f"{active_date}:{active_date}",
    ]
    print(f"[post-session-aggregator] running for {active_date}: {' '.join(cmd)}")
    try:
        completed = subprocess.run(
            cmd,
            cwd=str(PROJECT_DIR),
            check=False,
            timeout=300,
        )
        if completed.returncode != 0:
            print(
                f"[post-session-aggregator] aggregator exited with "
                f"code {completed.returncode}; canonical report may be stale."
            )
            return
    except subprocess.TimeoutExpired:
        print(
            "[post-session-aggregator] aggregator timed out after 300s; "
            "canonical report may be stale."
        )
        return
    except Exception as exc:  # pragma: no cover - best-effort path
        print(f"[post-session-aggregator] aggregator raised: {exc!r}")
        return

    # Echo the freshly-written daily-read so the operator sees today's
    # ranking right after the session ends without having to open the file.
    canonical_md = (
        PROJECT_DIR
        / "data"
        / "analysis_output"
        / "parallel_engine_comparison"
        / "parallel_engine_comparison.md"
    )
    try:
        text = canonical_md.read_text(encoding="utf-8", errors="replace")
    except OSError:
        print(
            "[post-session-aggregator] aggregator ran but canonical report "
            f"not readable at {canonical_md}."
        )
        return

    # Extract just the "Daily read" + "Per-config headline" sections.
    # Anything past the first "## Gate/candidate funnel" header is detail.
    sections: List[str] = []
    seen_daily = False
    for line in text.splitlines():
        if line.startswith("## Daily read") or line.startswith("## Per-config headline"):
            seen_daily = True
        elif line.startswith("## Gate/candidate funnel"):
            break
        if seen_daily:
            sections.append(line)
    if sections:
        print(f"[post-session-aggregator] daily read for {active_date}:")
        for line in sections:
            print(f"  {line}")
    else:
        print(
            f"[post-session-aggregator] aggregator ran; full report at {canonical_md}."
        )


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    # 2026-05-25: default expanded from 3 to 5 configs (added D and E).
    # 2026-05-26: default expanded from 5 to 10 configs (added F-J), each
    # mapped to one open Active or Hygiene roadmap question.
    # 2026-05-26 (later): K_line5p5_block added (11 total) -- ships
    # Hygiene #1 as a paper preset for A/B-test evidence vs A_current.
    # 2026-05-28: L_enforce_min_raw_095 (12) -- A/B-tests the calibration
    # edge-shaving recommendation (enforce_min_raw 0.90 -> 0.95).
    # 2026-05-28: M_under_paper (13) -- no-risk paper mirror of live UNDER;
    # accumulates the B4 paper-UNDER validation evidence.
    # 2026-06-01: N_extreme_edge_022 (14) -- A/B vs the 0.22 cap.
    # 2026-06-10: fleet pruned 14 -> 11 per the paired-delta audit:
    # E_tight_edge + G_loose_edge CONCLUDED (both directions of a +/-5pp
    # edge-floor move lose money; 0.15 floor is locally optimal),
    # N_extreme_edge_022 NULL (identical bet set to A_current -- its
    # "production runs at 1.0" premise was wrong). See the retirement
    # comments in PRESETS above for the full evidence.
    # 2026-06-11: O_nb_stage1 + P_alt_a_cache added (13 total) -- the
    # first model-version arms: Stage-1 cache swaps (NB tail / full
    # Alt-A) under the production calibrator, feeding the RF2 +
    # Active #8 decision with live-fire paper evidence.
    raw_configs = args.config or [
        "A_current", "B_cal_only", "C_raw", "D_scope_only",
        "F_no_dedup", "H_late_innings",
        "I_extreme_018", "J_no_phantom_filter", "K_line5p5_block",
        "L_enforce_min_raw_095", "M_under_paper",
        "O_nb_stage1", "P_alt_a_cache",
    ]
    configs = [_resolve_config(raw, Path(args.paper_root_prefix)) for raw in raw_configs]

    labels = [cfg.label for cfg in configs]
    if len(labels) != len(set(labels)):
        raise SystemExit(f"Duplicate config labels are not allowed: {labels}")

    active_date = args.date or _default_date(args.timezone)
    if args.market_data_mode == "per-engine":
        commands = [(cfg, build_engine_command(args, cfg)) for cfg in configs]
        exit_code = _run_per_engine_mode(args, configs, commands, active_date)
    else:
        exit_code = _run_shared_mode(args, configs, active_date)

    # Post-session aggregator hook. Runs whether engines exited normally
    # or via signal; fail-open so an aggregator error does not change
    # the launcher's exit code.
    try:
        _run_post_session_aggregator(args, configs, active_date)
    except Exception as exc:  # pragma: no cover - last-line safety net
        print(f"[post-session-aggregator] unexpected error: {exc!r}")

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
