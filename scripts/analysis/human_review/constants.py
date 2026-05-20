from pathlib import Path
from typing import Tuple

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
