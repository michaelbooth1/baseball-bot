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

CROSS_ARTIFACT_CONSISTENCY_PATHS: Tuple[Tuple[str, str], ...] = (
    ("stage1_cache", "cache/mlb_ou_cache.json"),
    ("stage2_cache", "cache/mlb_stage2_run_env.json"),
    ("stage3_v2_weights", "cache/team_offense_v2_weights.json"),
    ("calibrator_over",
     "data/analysis_output/calibration/signal_win_calibration.json"),
    ("calibrator_under",
     "data/analysis_output/calibration/signal_win_calibration_under.json"),
    ("walk_forward_cert",
     "data/analysis_output/walk_forward_certification/"
     "walk_forward_certification.json"),
    ("ev_policy_report",
     "data/analysis_output/ev_policy/ev_policy_report.json"),
    ("loss_attribution",
     "data/analysis_output/loss_attribution/loss_attribution_report.json"),
    ("stage1_shadow_override",
     "data/analysis_output/stage1_shadow_override/"
     "stage1_shadow_override_report.json"),
    ("stage1_cell_loss_attribution",
     "data/analysis_output/stage1_cell_loss_attribution/"
     "stage1_cell_loss_attribution.json"),
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
