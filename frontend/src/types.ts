/**
 * TypeScript types for the daily-review JSON shape produced by
 * `scripts/analysis/build_daily_human_review_report.py`. Only the
 * fields the frontend reads are typed; the JSON has many more
 * (16+ health blocks, deep diagnostics) and we treat the rest as
 * `unknown` to avoid type-drift churn when the analysis layer
 * adds new fields.
 */

export type ReviewIndex = {
  dates: string[];
  reviewsDir: string;
};

/**
 * Session JSON shape (subset of fields the weekly table reads).
 * Session files live in data/{live_trading,paper_trading}/sessions/
 * <date>_session.json and ALWAYS exist for any date the engine ran,
 * regardless of whether a daily_human_review was built for them.
 */
export type SessionFile = {
  date?: string;
  mode?: string;
  bets?: BetRow[];
  summary?: {
    total_profit?: number | null;
    total_staked?: number | null;
    win_rate?: number | null;
    total_bets?: number | null;
    settled?: number | null;
    [key: string]: unknown;
  };
  /** Frontend-only stamp added at load time by App.tsx so consumers
   *  (e.g., MultiEngineDayView) can map a loaded SessionFile back to
   *  its (modeFolder, configLabel) without an extra lookup. The
   *  underlying JSON on disk does NOT carry these fields. */
  _modeFolder?: string;
  _configLabel?: string;
};

export type SessionIndexEntry = {
  date: string;
  /** Folder slug. Legacy: `live`, `paper`. Multi-engine (2026-05-25+):
   *  `paper_<label>` (e.g., `paper_A_current`). */
  modeFolder: string;
  /** The session JSON's own `.mode` field (engine-authoritative).
   *  May differ from `modeFolder` when e.g. a dry_run session lives
   *  under `live_trading/`. */
  mode: string;
  /** Parallel-engine config label (e.g., `A_current`). Only present
   *  for sessions that ran under the multi-engine launcher; absent
   *  for legacy single-engine paper/live sessions. */
  configLabel?: string;
};

/** Top-level shape of `parallel_engine_comparison_<start>_<end>.json`
 *  produced by `scripts/analysis/aggregate_parallel_engines.py`. Only
 *  the fields the UI reads are typed; the JSON has many more (per-
 *  config funnel details, fine-state disagreements) that we treat as
 *  unknown to avoid type-drift churn. */
export type ParallelComparison = {
  generated_at_utc?: string;
  date_range?: { start?: string | null; end?: string | null };
  /** 2026-05-26: config used as the volume-index baseline.
   *  Defaults to "A_current" when present, else first alpha. */
  baseline_config_label?: string | null;
  configs?: Record<string, ParallelConfigPayload>;
  shared_candidate_disagreement?: {
    game_line?: ParallelDisagreementBlock;
    fine_state?: ParallelDisagreementBlock;
  };
  daily_read?: ParallelDailyRead;
};

export type ParallelConfigPayload = {
  headline?: ParallelHeadline;
  funnel?: {
    n_candidates?: number;
    by_decision?: Record<string, number>;
    top_decision_reasons?: Record<string, number>;
  };
  completeness?: {
    complete?: boolean;
    reasons?: string[];
  };
};

export type ParallelHeadline = {
  n_bets?: number | null;
  n_settled?: number | null;
  n_won?: number | null;
  win_rate?: number | null;
  total_staked?: number | null;
  total_profit?: number | null;
  roi?: number | null;
  max_drawdown?: number | null;
  mean_fair_value?: number | null;
  mean_entry_ask?: number | null;
  mean_fair_value_settled?: number | null;
  mean_entry_ask_settled?: number | null;
  stake_weighted_fair_value?: number | null;
  stake_weighted_entry_ask?: number | null;
  stake_weighted_win_rate?: number | null;
  edge_over_market_actual_minus_ask?: number | null;
  edge_over_market_settled_actual_minus_ask?: number | null;
  edge_over_market_stake_weighted_actual_minus_ask?: number | null;
  // 2026-05-26 normalization fields. Lets the UI read F_no_dedup
  // (high-volume) against A_current (production-mirror) on equal
  // per-bet footing instead of letting volume distort raw totals.
  profit_per_settled_bet?: number | null;
  n_unique_game_lines?: number | null;
  n_settled_unique_game_lines?: number | null;
  bets_per_unique_game_line?: number | null;
  baseline_label?: string | null;
  volume_index_vs_baseline?: number | null;
  settled_index_vs_baseline?: number | null;
};

export type ParallelDisagreementBlock = {
  counts?: {
    keys_compared?: number;
    unanimous_skip?: number;
    unanimous_trade?: number;
    split?: number;
    partial_coverage?: number;
  };
  /** 2026-05-31: renamed from `splits` to match what the aggregator
   *  actually emits in the JSON (`split_examples`). The pre-rename
   *  type read `splits` and showed an empty table even when
   *  `counts.split` was non-zero. */
  split_examples?: Array<{
    key: string;
    decisions?: Record<string, string>;
    outcome?: {
      final_away?: number | null;
      final_home?: number | null;
      final_total?: number | null;
      over_hit?: boolean | null;
      won?: boolean | null;
    };
  }>;
};

export type ParallelDailyRead = {
  best_roi_config?: string | null;
  best_roi?: number | null;
  lowest_drawdown_config?: string | null;
  lowest_drawdown?: number | null;
  // 2026-05-26 normalization: per-bet leader independent of volume.
  best_profit_per_settled_bet_config?: string | null;
  best_profit_per_settled_bet?: number | null;
  baseline_config_label?: string | null;
  game_line_splits?: number;
  fine_state_splits?: number;
  sample_flags?: string[];
};

export type ParallelComparisonIndex = {
  ranges: string[];
  parallelDir?: string;
};

export type SessionIndex = {
  sessions: SessionIndexEntry[];
  errors?: Array<{ modeFolder: string; detail: string }>;
};

export type SessionSummary = {
  orders_placed?: number | null;
  orders_filled?: number | null;
  wins?: number | null;
  losses?: number | null;
  total_profit?: number | null;
  roi?: number | null;
  total_bets?: number | null;
  settled?: number | null;
};

export type BetTotals = {
  count?: number;
  filled?: number;
  wins?: number;
  losses?: number;
  profit?: number;
  roi?: number | null;
  win_rate?: number | null;
  avg_entry_ask?: number | null;
  avg_limit_price?: number | null;
  avg_fair_value?: number | null;
  by_side?: {
    over?: BetSideTotals;
    under?: BetSideTotals;
  };
};

export type BetSideTotals = {
  count: number;
  filled: number;
  wins: number;
  losses: number;
  profit: number;
  win_rate: number | null;
  roi: number | null;
};

export type BetRow = {
  bet_id?: string;
  game?: string;
  game_pk?: number;
  away_abbrev?: string;
  home_abbrev?: string;
  line?: string;
  side?: string;
  entry_ask?: number | null;
  fair_value?: number | null;
  edge?: number | null;
  inning?: number;
  inning_state?: string;
  stake?: number | null;
  final_total?: number | null;
  settled?: boolean | null;
  won?: boolean | null;
  profit?: number | null;
  placed_at?: string | null;
  settled_at?: string | null;
  inferred_state_base_empirical?: number | null;
  fair_value_alt_empirical?: number | null;
};

/**
 * Health blocks share a common surface: a list of free-form `alerts`
 * strings. We don't type each block's deep fields; the UI counts
 * alerts and surfaces status (green/yellow/red) based on the count.
 */
export type HealthBlock = {
  alerts?: string[];
  status?: string;
  // Pass-through for block-specific fields the dedicated panels read.
  [key: string]: unknown;
};

export type DailyReview = {
  schema_version?: number;
  generated_at_utc?: string;
  session_date?: string;
  mode?: string;
  session_summary?: SessionSummary;
  bet_totals?: BetTotals;
  bets?: BetRow[];
  notes?: string[];
  source_files?: {
    session?: string | null;
    candidate_rollup?: string | null;
    log?: string | null;
  };
  candidate_rollup_compact?: {
    attempted_rows?: number;
    written_rows?: number;
    by_decision?: Record<string, number>;
  };
  // Health blocks (all optional; presence varies by ship date)
  calibration_health?: HealthBlock;
  fill_rate_health?: HealthBlock;
  signal_quality_health?: HealthBlock;
  regime_mix_health?: HealthBlock;
  cohort_roi_health?: HealthBlock;
  cohort_calibration_health?: HealthBlock;
  loss_attribution_health?: HealthBlock;
  cache_lineage_freshness_health?: HealthBlock;
  stage1_cell_loss_health?: HealthBlock;
  stage1_shadow_override_health?: HealthBlock;
  cross_artifact_consistency_health?: HealthBlock;
  stage1_alt_a_staging_health?: HealthBlock;
  promotion_lag_health?: HealthBlock;
  under_emission_health?: HealthBlock;
  under_outcomes_counterfactual_health?: HealthBlock;
  concept_drift_health?: HealthBlock;
  drift_in_drift_health?: HealthBlock;
  daemon_readiness_health?: HealthBlock;
  under_book_coverage_health?: HealthBlock;
  settlement_truth_health?: HealthBlock;
  fast_demote_health?: HealthBlock;
  gate_counterfactual_health?: HealthBlock;
  log_health?: HealthBlock;
  reconciler_summary?: HealthBlock;
};
