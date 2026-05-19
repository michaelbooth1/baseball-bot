import type { FC } from "react";
import type { DailyReview, HealthBlock } from "../types";

type Props = {
  review: DailyReview;
};

/**
 * The daily review carries 17+ health blocks. Most days most blocks
 * are quiet. This grid surfaces each block as a small status card
 * colored by alert-count:
 *   - green  (0 alerts AND block present)
 *   - yellow (1-2 alerts)
 *   - red    (3+ alerts)
 *   - gray   (block missing from this review)
 *
 * Clicking a card expands it to show the raw alerts list.
 */

// Order chosen so the operationally-important blocks lead.
const BLOCK_ORDER: Array<{ key: keyof DailyReview; label: string }> = [
  { key: "calibration_health", label: "Calibration" },
  { key: "cohort_calibration_health", label: "Cohort calibration" },
  { key: "cohort_roi_health", label: "Cohort ROI" },
  { key: "fill_rate_health", label: "Fill rate" },
  { key: "signal_quality_health", label: "Signal quality" },
  { key: "regime_mix_health", label: "Regime mix" },
  { key: "concept_drift_health", label: "Concept drift" },
  { key: "drift_in_drift_health", label: "Drift-in-drift" },
  { key: "loss_attribution_health", label: "Loss attribution" },
  { key: "stage1_shadow_override_health", label: "Stage-1 shadow override" },
  { key: "stage1_cell_loss_health", label: "Stage-1 cell loss" },
  { key: "stage1_alt_a_staging_health", label: "Stage-1 Alt-A staging" },
  { key: "under_emission_health", label: "UNDER emission" },
  { key: "under_outcomes_counterfactual_health", label: "UNDER outcomes" },
  { key: "under_book_coverage_health", label: "UNDER book coverage" },
  { key: "gate_counterfactual_health", label: "Gate counterfactual" },
  { key: "settlement_truth_health", label: "Settlement truth" },
  { key: "fast_demote_health", label: "Fast demote" },
  { key: "cache_lineage_freshness_health", label: "Cache lineage" },
  { key: "cross_artifact_consistency_health", label: "Cross-artifact consistency" },
  { key: "promotion_lag_health", label: "Promotion lag" },
  { key: "daemon_readiness_health", label: "Daemon readiness" },
  { key: "reconciler_summary", label: "Reconciler" },
];

export const HealthStatusGrid: FC<Props> = ({ review }) => {
  return (
    <section className="card">
      <h2 className="card-title">Health blocks ({BLOCK_ORDER.length})</h2>
      <div className="health-grid">
        {BLOCK_ORDER.map(({ key, label }) => {
          const block = review[key] as HealthBlock | undefined;
          return <HealthCard key={String(key)} label={label} block={block} />;
        })}
      </div>
    </section>
  );
};

const HealthCard: FC<{ label: string; block?: HealthBlock }> = ({
  label,
  block,
}) => {
  if (!block) {
    return (
      <div className="health-card health-card-missing">
        <div className="health-label">{label}</div>
        <div className="health-status">—</div>
      </div>
    );
  }
  const alertCount = Array.isArray(block.alerts) ? block.alerts.length : 0;
  let level: "ok" | "warn" | "alert" = "ok";
  if (alertCount >= 3) level = "alert";
  else if (alertCount >= 1) level = "warn";
  return (
    <details className={"health-card health-card-" + level}>
      <summary>
        <div className="health-label">{label}</div>
        <div className="health-status">
          {alertCount === 0 ? "ok" : `${alertCount} alert${alertCount === 1 ? "" : "s"}`}
        </div>
      </summary>
      {alertCount > 0 && (
        <ul className="health-alerts">
          {(block.alerts ?? []).map((a, i) => (
            <li key={i}>{a}</li>
          ))}
        </ul>
      )}
    </details>
  );
};
