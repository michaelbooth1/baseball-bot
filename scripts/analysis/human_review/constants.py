from pathlib import Path
from typing import Any, Dict, Tuple

PROJECT_DIR = Path(__file__).resolve().parents[3]
DEFAULT_SESSIONS_DIR = PROJECT_DIR / "data" / "live_trading" / "sessions"
DEFAULT_CANDIDATE_DIR = PROJECT_DIR / "data" / "live_trading" / "candidate_universe"
DEFAULT_LOG_DIR = PROJECT_DIR / "logs" / "real-logs"
DEFAULT_OUTPUT_ROOT = PROJECT_DIR / "data" / "analysis_output" / "daily_human_review"
DEFAULT_CALIBRATION_ARTIFACT = (
    PROJECT_DIR / "data" / "analysis_output" / "calibration" / "signal_win_calibration.json"
)
DEFAULT_CALIBRATION_ARTIFACT_UNDER = (
    PROJECT_DIR / "data" / "analysis_output" / "calibration"
    / "signal_win_calibration_under.json"
)

CALIBRATION_NEAR_IDENTITY_DELTA = 0.005
CALIBRATION_LOW_APPLIED_SHARE = 0.05
CALIBRATION_SHADOW_MODE_DOMINANT_SHARE = 0.95
CALIBRATION_STALE_AGE_DAYS = 14

# Calibrator-enforce shipment-effect observability (2026-05-20).
# Surfaces "how many bets did band-gated calibrator-enforce block (or
# would block, in counterfactual mode)?" so the operator can see the
# effect of the 2026-05-19 enforce flip within one paper session.
#
# CALIBRATOR_ENFORCE_BAND_GATE_THRESHOLD: the raw_fv threshold below
# which the calibrator is NOT applied even under enforce. Matches
# DEFAULT_PROB_CALIBRATION_ENFORCE_MIN_RAW in signal_config.py. If you
# change one, change both.
CALIBRATOR_ENFORCE_BAND_GATE_THRESHOLD = 0.90
# Edge thresholds the gate uses to decide block vs pass. Mirrors
# DEFAULT_EDGE_THRESHOLD / DEFAULT_EDGE_THRESHOLD_HIGH_LINE.
CALIBRATOR_ENFORCE_MIN_EDGE_LOW_LINE = 0.15
CALIBRATOR_ENFORCE_MIN_EDGE_HIGH_LINE = 0.16
CALIBRATOR_ENFORCE_HIGH_LINE_CUTOFF = 8.5
# Alert thresholds.
# Fire when the calibrator-enforce blocks >= this share of would-trade
# candidates; might mean the gate is too aggressive for the current
# regime (calibrator over-corrects).
CALIBRATOR_ENFORCE_HIGH_BLOCK_RATE_ALERT = 0.80
# Fire when enforce blocks 0 bets despite many would-trades being in
# the band-gated range; suggests the calibrator is returning identity
# or the gate is bypassed.
CALIBRATOR_ENFORCE_MIN_BAND_GATED_CANDIDATES_FOR_ZERO_ALERT = 10
# Volume drop vs trailing-7d baseline that triggers an alert.
CALIBRATOR_ENFORCE_VOLUME_DROP_ALERT_PP = 0.50
# Need at least this many days of trailing baseline before computing
# the volume-drop alert (avoids false alarms on cold-start).
CALIBRATOR_ENFORCE_BASELINE_MIN_DAYS = 3
CALIBRATOR_ENFORCE_BASELINE_WINDOW_DAYS = 7

# Would-block outcome tracking (2026-05-20 v2 ship). For each
# would-block candidate (shadow counterfactual) or attributed-block
# candidate (enforce attribution), look up the realized game outcome
# and compute "did this block save a loss or mute a winner?"
# Paper-mode default stake is $10/bet; used for counterfactual P&L.
CALIBRATOR_ENFORCE_BLOCKED_OUTCOMES_DEFAULT_STAKE = 10.0
# Fire when WR among settled blocks >= this on >=
# CALIBRATOR_ENFORCE_BLOCKED_OUTCOMES_MIN_FOR_ALERT settled outcomes
# -- enforce may be muting winners. 0.60 because break-even at typical
# post-cal ask 0.75 is ~75% WR; 60%+ realized means the blocked set
# is approaching profitable in expectation.
CALIBRATOR_ENFORCE_BLOCKED_WR_MUTING_WINNERS = 0.60
CALIBRATOR_ENFORCE_BLOCKED_OUTCOMES_MIN_FOR_ALERT = 5
# Fire when cumulative counterfactual saved P&L is negative on
# >= MIN_FOR_ALERT settled outcomes -- enforce is blocking but the
# blocked set's expected value was positive.
CALIBRATOR_ENFORCE_BLOCKED_NEGATIVE_SAVE_ALERT = 0.0

DRIFT_TRAILING_WINDOW_DAYS = 7
DRIFT_MIN_TODAY_SAMPLE = 3
DRIFT_MIN_BASELINE_SAMPLE = 10
DRIFT_FILL_RATE_DROP_PP = 0.20
DRIFT_WIN_RATE_DROP_PP = 0.20
DRIFT_ZERO_DAY_MIN_SAMPLE = 5
DRIFT_WILSON_Z = 1.645
DRIFT_REGIME_MIX_TVD = 0.30
RECONCILER_HIGH_SHARE = 0.10

COHORT_ROI_TRAILING_WINDOW_DAYS = 7
COHORT_ROI_BASELINE_WINDOW_DAYS = 30
COHORT_ROI_MIN_BETS_FOR_ALERT = 5
COHORT_ROI_LOSING_THRESHOLD = -0.10
COHORT_ROI_REGIME_DELTA = 0.15

COHORT_CALIBRATION_WINDOW_DAYS = 7
COHORT_CALIBRATION_MIN_N_FOR_ALERT = 30
COHORT_CALIBRATION_GAP_RATIO_ALERT = 2.0
COHORT_CALIBRATION_MIN_AGGREGATE_GAP = 0.01
COHORT_CALIBRATION_AGGREGATE_GAP_ALERT = 0.10
COHORT_CALIBRATION_AGGREGATE_MIN_N = 15

DEFAULT_CONCEPT_DRIFT_REPORT = (
    PROJECT_DIR / "data" / "analysis_output" / "concept_drift" / "concept_drift_report.json"
)
DEFAULT_DRIFT_IN_DRIFT_REPORT = (
    PROJECT_DIR / "data" / "analysis_output" / "concept_drift" / "drift_in_drift_report.json"
)
DEFAULT_DAEMON_RETROSPECTIVE_REPORT = (
    PROJECT_DIR / "data" / "analysis_output" / "daemon_retrospective" / "daemon_retrospective.json"
)
CONCEPT_DRIFT_STALE_AGE_DAYS = 14
DRIFT_IN_DRIFT_STALE_AGE_DAYS = 14
DAEMON_RETROSPECTIVE_STALE_AGE_DAYS = 14
DAEMON_STALENESS_THRESHOLD_DAYS = 60

DEFAULT_PROMOTION_EVENTS_LOG = (
    PROJECT_DIR / "data" / "analysis_output" / "promotion_events.jsonl"
)
PROMOTION_ATTRIBUTION_WINDOW_DAYS = 14

DEFAULT_SETTLEMENT_TRUTH_REPORT = (
    PROJECT_DIR / "data" / "analysis_output" / "settlement_truth"
    / "settlement_truth_report.json"
)
SETTLEMENT_TRUTH_STALE_AGE_DAYS = 14
STALE_FILLED_ALERT_THRESHOLD = 1
MISSING_MLB_DATA_RATE_ALERT_THRESHOLD = 0.10

DEFAULT_MODEL_MATURITY_REPORT = (
    PROJECT_DIR / "data" / "analysis_output" / "model_maturity" / "model_maturity_report.json"
)
UNDER_BOOK_COVERAGE_WARN_THRESHOLD = 0.50
UNDER_BOOK_COVERAGE_STALE_AGE_DAYS = 14

DEFAULT_GATE_COUNTERFACTUAL_REPORT = (
    PROJECT_DIR / "data" / "analysis_output" / "gate_counterfactual"
    / "gate_counterfactual_report.json"
)
GATE_COUNTERFACTUAL_STALE_AGE_DAYS = 14

DEFAULT_LOSS_ATTRIBUTION_REPORT = (
    PROJECT_DIR / "data" / "analysis_output" / "loss_attribution"
    / "loss_attribution_report.json"
)
LOSS_ATTRIBUTION_STALE_AGE_DAYS = 14

DEFAULT_STAGE1_CELL_LOSS_REPORT = (
    PROJECT_DIR / "data" / "analysis_output"
    / "stage1_cell_loss_attribution"
    / "stage1_cell_loss_attribution.json"
)
STAGE1_CELL_LOSS_STALE_AGE_DAYS = 14

DEFAULT_STAGE1_SHADOW_OVERRIDE_REPORT = (
    PROJECT_DIR / "data" / "analysis_output"
    / "stage1_shadow_override"
    / "stage1_shadow_override_report.json"
)
STAGE1_SHADOW_OVERRIDE_STALE_AGE_DAYS = 14
STAGE1_CELL_LOSS_MIN_ABS_BIAS = 0.05
STAGE1_CELL_LOSS_FALLBACK_RATE_NOTES_FLOOR = 0.50

# 2026-05-21 (P1b): refresh-staleness alert. The daily refresh
# produces a `<date>_startup_refresh.json` artifact under
# DEFAULT_STARTUP_REFRESH_DIR. If the newest artifact's effective
# date is older than REFRESH_STALENESS_HOURS_WARN, the daily-review
# block fires an alert. Caught a real outage on 2026-05-20/21 where
# the refresh hadn't fired for 2 days but cross-artifact + drift
# alerts were the only downstream symptoms.
DEFAULT_STARTUP_REFRESH_DIR = (
    PROJECT_DIR / "data" / "analysis_output" / "startup_refresh"
)
REFRESH_STALENESS_HOURS_WARN = 36.0   # 1 missed day is OK
REFRESH_STALENESS_HOURS_ALERT = 60.0  # 2+ missed days is an outage

DEFAULT_STAGE1_CACHE_PATH = PROJECT_DIR / "cache" / "mlb_ou_cache.json"
DEFAULT_STAGE2_CACHE_PATH = PROJECT_DIR / "cache" / "mlb_stage2_run_env.json"
DEFAULT_STAGE3_V2_WEIGHTS_PATH = (
    PROJECT_DIR / "cache" / "team_offense_v2_weights.json"
)
CACHE_LINEAGE_BUILD_AGE_WARN_DAYS = 14

DEFAULT_STAGE1_ALT_A_STAGING_PATH = (
    PROJECT_DIR / "cache" / "mlb_ou_cache_alt_a.staging.json"
)
STAGE1_ALT_A_STAGING_AGE_WARN_DAYS = 14

PROMOTION_LAG_LEVERS: Tuple[Tuple[str, str], ...] = (
    ("stage1", "cache/mlb_ou_cache.json"),
    ("stage2", "cache/mlb_stage2_run_env.json"),
    ("stage3_v2", "cache/team_offense_v2_weights.json"),
    ("stake_scaling", "cache/live_engine_overrides.json"),
    ("gate_threshold", "cache/live_engine_overrides.json"),
)
PROMOTION_LAG_SESSION_ROOTS: Tuple[str, ...] = (
    "data/live_trading/sessions",
    "data/paper_trading/sessions",
)
PROMOTION_LAG_PENDING_HOURS_WARN = 24.0

# Hygiene #23 (2026-05-20): Known model-upgrade dates with the
# features whose distribution they materially shifted. Used by
# _concept_drift_health to annotate PSI-major alerts as "attributable
# to a planned upgrade" vs "real regime change" — the daily review
# was generating alarming false-positive drift alerts after every
# model upgrade because PSI's trailing baseline straddles the
# upgrade date.
#
# `affected_features`: list of feature names whose distribution shifts
#   when this upgrade ships. `direct` = the upgrade modifies the
#   model layer producing this feature. `indirect` = the upgrade
#   modifies a downstream/upstream layer that filters which rows
#   reach this feature (e.g., base_fair_value's distribution shifts
#   when Stage-2 deltas change because the bet-gate accepts a
#   different sample mix).
#
# `description`: short string surfaced in the alert reword.
#
# When adding a new entry: capture the ship date precisely (look for
# `cache/*.pre_*_<date>.bak` backup files or the promote.py audit
# rows). An attribution stale by a week is fine; off by a month
# isn't (would mis-attribute a real regime change).
MODEL_UPGRADES: Tuple[Dict[str, Any], ...] = (
    {
        "date": "2026-05-08",
        "name": "TR21",
        "description": (
            "Stage-2 density_alt + hr_factor families added "
            "(cache/mlb_stage2_run_env.json.pre_density_alt_hr_factor_2026_05_08.bak)"
        ),
        "affected_features": {
            "stage2_run_env_delta": "direct",
            "base_fair_value": "indirect (bet-gate sample mix)",
        },
    },
    {
        "date": "2026-05-08",
        "name": "TR20",
        "description": (
            "Stage-3 v2 (team_offense_v2) shipped per ROADMAP "
            "Recently-Completed entry"
        ),
        "affected_features": {
            "team_offense_delta": "direct",
            "base_fair_value": "indirect (bet-gate sample mix)",
        },
    },
)

UNDER_COVERAGE_RATE_LOW_WARN = 0.50
UNDER_COVERAGE_MIN_N_FOR_ALERT = 50
UNDER_SHADOW_UNDER_RATE_HIGH_WARN = 0.50
UNDER_SHADOW_UNDER_MIN_N_FOR_ALERT_HIGH = 20
UNDER_SHADOW_UNDER_RATE_LOW_WARN = 0.02
UNDER_SHADOW_UNDER_MIN_N_FOR_ALERT_LOW = 100
UNDER_FV_BUCKETS: Tuple[Tuple[str, float, float], ...] = (
    ("0.00-0.20", 0.00, 0.20),
    ("0.20-0.40", 0.20, 0.40),
    ("0.40-0.60", 0.40, 0.60),
    ("0.60-0.80", 0.60, 0.80),
    ("0.80-1.00", 0.80, 1.00),
)

UNDER_OUTCOMES_DEFAULT_STAKE = 10.0
UNDER_OUTCOMES_PROFITABLE_ROI_WARN = 0.05
UNDER_OUTCOMES_UNPROFITABLE_ROI_WARN = -0.05
UNDER_OUTCOMES_MIN_N_FOR_ALERT = 30
UNDER_OUTCOMES_TRAILING_DAYS = 7
UNDER_OUTCOMES_TRAILING_MIN_N_FOR_ALERT = 50

# Phase C-paper follow-up (2026-05-27): UNDER paper-bet B4 milestone
# dashboard. Tracks the 5 ROADMAP B4 verdict conditions against
# ACTUAL `side="under"` paper bets (the existing
# `_under_outcomes_counterfactual_health` block tracks SHADOW
# counterfactuals, which does not advance B4). The block walks both
# paper_root/sessions/ and live_root/sessions/ across the trailing
# window so an operator running the live engine with
# `--under-mode paper` accumulates evidence the same way the paper
# engine does.
DEFAULT_PAPER_SESSIONS_DIR = (
    PROJECT_DIR / "data" / "paper_trading" / "sessions"
)
# 2026-06-10: extra paper roots the B4 scanner walks IN ADDITION to
# the default paper + live roots. The parallel-engine fleet writes to
# data/paper_<label>/sessions/, NOT data/paper_trading/sessions/, so
# the M_under_paper preset had been accumulating UNDER paper bets
# since 2026-05-30 that were invisible to the B4 milestone (dashboard
# read 0/60 sessions while evidence existed). Bets are deduped by
# bet_id across all roots, so an identical signal counted by two
# roots still counts once.
B4_EXTRA_PAPER_SESSION_ROOTS: tuple = (
    PROJECT_DIR / "data" / "paper_M_under_paper" / "sessions",
)
# 2026-06-11: fleet paired-delta block. The parallel-engine fleet's
# daily aggregator publishes per-engine marginal ROI tables, but all
# engines see the same games -- the decision information lives in the
# DELTA bets (bets one config took that the baseline didn't, and vice
# versa). This block computes the paired comparison per preset vs the
# baseline so fleet conclusions surface in the daily review instead of
# requiring a manual audit (the 2026-06-10 E/G/N retirements were all
# discoverable weeks earlier from this exact arithmetic).
FLEET_DATA_ROOT = PROJECT_DIR / "data"
FLEET_BASELINE_LABEL = "A_current"
# Legacy / non-fleet paper roots that must not be compared as presets.
FLEET_EXCLUDED_ROOT_NAMES: tuple = ("paper_trading",)
# Retired fleet presets (concluded experiments). Their
# data/paper_<label> roots persist until the last session ages out of
# the trailing window (~30 days), during which the paired-delta block
# would keep emitting stale verdicts — most visibly N_extreme_edge_022's
# daily DEAD alert (retired 2026-06-10 but still firing on 2026-06-13
# because its sessions sit inside the 30d window). Excluding them
# explicitly stops a concluded experiment generating noise the day it's
# retired instead of ~30 days later. Keep in sync with the `RETIRED`
# comments in launch_parallel_engines.py PRESETS.
FLEET_RETIRED_ROOT_NAMES: tuple = (
    "paper_E_tight_edge",         # retired 2026-06-10 (edge floor +5pp concluded)
    "paper_G_loose_edge",         # retired 2026-06-10 (edge floor -5pp concluded)
    "paper_N_extreme_edge_022",   # retired 2026-06-10 (null experiment, 0 delta)
)
FLEET_PAIRED_DELTA_TRAILING_DAYS = 30
# DEAD: enough shared days to judge, and near-zero delta-bet flow --
# the preset produces no distinct decisions vs baseline (N_extreme_
# edge_022 signature: 10 days, 0 delta bets).
FLEET_DEAD_MIN_SHARED_DAYS = 7
FLEET_DEAD_MAX_DELTA_FLOW_PER_DAY = 0.3
# CONCLUSIVE: Welch-t on unique-cohort per-bet profit clears 1.96
# with a minimum combined delta-bet sample.
FLEET_CONCLUSIVE_T = 1.96
FLEET_CONCLUSIVE_MIN_DELTA_N = 20
# TRENDING: directional but not yet significant.
FLEET_TRENDING_T = 1.0
FLEET_TRENDING_MIN_DELTA_N = 10

# B4 thresholds (from ROADMAP B4 entry; change here AND in ROADMAP
# together so the doc and the verdict stay in sync).
B4_MILESTONE_TRAILING_DAYS = 60
B4_MILESTONE_MIN_SESSIONS = 60
B4_MILESTONE_MIN_SETTLED = 150
B4_MILESTONE_MIN_ROI = 0.0
B4_MILESTONE_CALIBRATION_TOLERANCE_PP = 5.0
B4_MILESTONE_DRIFT_ALERT_LOOKBACK_DAYS = 7
B4_MILESTONE_DRIFT_PERSISTENCE_THRESHOLD = 3
# Minimum n_settled for ROI / calibration / drift alerts to fire
# (avoids noisy alerts on tiny samples while still showing per-
# condition status in the JSON for the operator).
B4_MILESTONE_MIN_N_FOR_FAILURE_ALERT = 30

# 2026-06-03: extended from 2-tuple (label, path) to 3-tuple
# (label, path, rebuilt_each_refresh) to suppress transient STALE
# alerts on artifacts that the same refresh will resolve.
#
# Root cause of the noise: `daily_human_review` runs at step 14 of the
# refresh, BEFORE the artifacts at steps 17-38 get rebuilt. So the
# consistency check at step 14 sees yesterday's lineage vs whatever
# inputs were rewritten by earlier steps -- a transient state that
# would be resolved by end-of-refresh.
#
# For `rebuilt_each_refresh=True` artifacts (calibrator, walk-forward,
# etc.), STALE alerts are downgraded to informational notes -- they
# self-resolve by end of the same refresh.
#
# For `rebuilt_each_refresh=False` artifacts (`stage3_v2_weights` is
# promotion-gated, only rebuilt on operator-approved promotion), STALE
# alerts continue to fire because the operator needs to act
# (`promote.py stage3-v2` or wait for the daemon in act mode).
#
# The 3-tuple shape is back-compat: the consumer's defensive code in
# `_cross_artifact_consistency_health` falls back to True if a spec
# is a 2-tuple, preserving the old behavior for any external callers.
CROSS_ARTIFACT_CONSISTENCY_PATHS: Tuple[Tuple[str, str, bool], ...] = (
    ("stage1_cache", "cache/mlb_ou_cache.json", True),
    ("stage2_cache", "cache/mlb_stage2_run_env.json", True),
    # stage3_v2_weights is PROMOTION-GATED (operator runs
    # `promote.py stage3-v2`), not rebuilt by the daily refresh, so a
    # stale alert IS real signal for the operator.
    ("stage3_v2_weights", "cache/team_offense_v2_weights.json", False),
    ("calibrator_over",
     "data/analysis_output/calibration/signal_win_calibration.json", True),
    ("calibrator_under",
     "data/analysis_output/calibration/signal_win_calibration_under.json",
     True),
    ("walk_forward_cert",
     "data/analysis_output/walk_forward_certification/"
     "walk_forward_certification.json", True),
    ("ev_policy_report",
     "data/analysis_output/ev_policy/ev_policy_report.json", True),
    ("loss_attribution",
     "data/analysis_output/loss_attribution/loss_attribution_report.json",
     True),
    ("stage1_shadow_override",
     "data/analysis_output/stage1_shadow_override/"
     "stage1_shadow_override_report.json", True),
    ("stage1_cell_loss_attribution",
     "data/analysis_output/stage1_cell_loss_attribution/"
     "stage1_cell_loss_attribution.json", True),
)
LOSS_ATTRIBUTION_NOTES_MIN_ABS_BIAS = 0.05
LOSS_ATTRIBUTION_NOTES_MIN_SHARE = 0.50
GATE_COUNTERFACTUAL_NOTES_MIN_DELTA_USD = 40.0
GATE_COUNTERFACTUAL_NOTES_MAX_ALERTS = 3

LOG_PATTERNS = {
    "schedule_refreshed": "Schedule refreshed",
    "polling_token_books": "Polling ",
    "wrote_tick_snapshots": "Wrote ",
    "retiring_book_polling": "Retiring book polling",
    "warnings": "WARNING",
    "errors": "ERROR",
    "fresh_book_unavailable": "fresh execution book missing",
}
