import type { FC } from "react";
import type { DailyReview } from "../types";

type Props = {
  /** All loaded reviews (newest first) so milestones aggregate across days. */
  reviews: DailyReview[];
};

/**
 * Visual progress bars for the 3 active data-accumulation gates:
 *
 *   1. Active #1 walk-forward (Stage-2/3 re-certification): needs
 *      ~150 filled bets / 30 dates with post-TR20 data. Count from
 *      `bet_totals.filled` summed across loaded reviews.
 *
 *   2. Phase A5 -> B4 (UNDER paper-bet validation milestone): needs
 *      60 sessions of UNDER signal data. Reads
 *      `under_outcomes_counterfactual_health.trailing_7d
 *      .n_dates_with_data` from the latest review for the running
 *      count, but TODO: the proper count would be cumulative across
 *      ALL dates the operator runs A5 (the trailing-7d view
 *      undercounts beyond 7 days). v1: trailing surface only.
 *
 *   3. Stage-1 Alt-A 30d shadow (Active #8 ENFORCE-flip evidence):
 *      needs ~150 settled bets in the trailing-30d window. Reads
 *      `stage1_shadow_override_health.trailing_30d.n_bets` from
 *      the latest review.
 *
 * Each milestone shows: title, current vs target, fill bar, blurb.
 */
export const ProgressMilestones: FC<Props> = ({ reviews }) => {
  const latest = reviews[0];

  // 1. Active #1 walk-forward — sum filled bets + count unique dates
  let totalFilled = 0;
  const dates = new Set<string>();
  for (const r of reviews) {
    const filled =
      r.bet_totals?.filled ?? r.session_summary?.orders_filled ?? 0;
    if (filled > 0) {
      totalFilled += filled;
      if (r.session_date) dates.add(r.session_date);
    }
  }
  const walkForwardBetsPct = Math.min(1, totalFilled / 150);
  const walkForwardDatesPct = Math.min(1, dates.size / 30);

  // 2. B4 UNDER milestone — trailing-7d block reports
  //    `n_dates_with_data` of trailing_days (7) but the milestone
  //    is 60 SESSIONS. For v1 we just read what trailing reports
  //    and surface it as "X / 60 sessions, last 7d showed N".
  const underTrailing =
    latest?.under_outcomes_counterfactual_health?.["trailing_7d"];
  const underDatesWithData = isObject(underTrailing)
    ? (underTrailing.n_dates_with_data as number | undefined) ?? 0
    : 0;
  const underSettledTotal = isObject(underTrailing)
    ? (underTrailing.n_settled_total as number | undefined) ?? 0
    : 0;
  const b4Pct = Math.min(1, underDatesWithData / 60);

  // 3. Stage-1 Alt-A 30d shadow — trailing-30d window sample size
  const shadowTrailing =
    latest?.stage1_shadow_override_health?.["trailing_30d"];
  const shadowNBets = isObject(shadowTrailing)
    ? (shadowTrailing.n_bets as number | undefined) ?? 0
    : 0;
  const altaPct = Math.min(1, shadowNBets / 150);

  return (
    <section className="card">
      <h2 className="card-title">Progress toward decision-grade evidence</h2>
      <p className="card-meta">
        Each milestone is a sample-size gate before the relevant
        promote-or-tune decision becomes actionable. Loaded {reviews.length}{" "}
        review{reviews.length === 1 ? "" : "s"}.
      </p>
      <ul className="milestone-list">
        <Milestone
          title="Active #1 — post-TR20 walk-forward"
          subtitle="Re-certifies enforced gates against fresh data"
          progress={[
            {
              label: "filled bets",
              current: totalFilled,
              target: 150,
              pct: walkForwardBetsPct,
            },
            {
              label: "session dates",
              current: dates.size,
              target: 30,
              pct: walkForwardDatesPct,
            },
          ]}
        />
        <Milestone
          title="Phase A5 → B4 — UNDER paper-bet validation"
          subtitle="60 sessions of UNDER shadow data before any UNDER paper-bet flip"
          progress={[
            {
              label: "sessions with UNDER data (trailing-7d view)",
              current: underDatesWithData,
              target: 60,
              pct: b4Pct,
            },
            {
              label: "settled shadow_under (trailing-7d)",
              current: underSettledTotal,
              target: 350,
              pct: Math.min(1, underSettledTotal / 350),
            },
          ]}
        />
        <Milestone
          title="Stage-1 Alt-A 30d shadow (Active #8)"
          subtitle="Settled bets backing Alt-A cohort breakdown evidence"
          progress={[
            {
              label: "trailing-30d settled bets",
              current: shadowNBets,
              target: 150,
              pct: altaPct,
            },
          ]}
        />
      </ul>
    </section>
  );
};

type ProgressBar = {
  label: string;
  current: number;
  target: number;
  pct: number;
};

const Milestone: FC<{
  title: string;
  subtitle: string;
  progress: ProgressBar[];
}> = ({ title, subtitle, progress }) => {
  return (
    <li className="milestone">
      <h3 className="milestone-title">{title}</h3>
      <p className="milestone-subtitle">{subtitle}</p>
      <div className="bar-stack">
        {progress.map((p) => {
          const ready = p.pct >= 1;
          return (
            <div className="bar-row" key={p.label}>
              <div className="bar-label">
                <span>{p.label}</span>
                <span className={ready ? "bar-current-ready" : "bar-current"}>
                  {p.current}/{p.target}
                  {ready ? " ✓" : ""}
                </span>
              </div>
              <div className="bar-track">
                <div
                  className={"bar-fill" + (ready ? " bar-fill-ready" : "")}
                  style={{ width: `${Math.round(p.pct * 100)}%` }}
                />
              </div>
            </div>
          );
        })}
      </div>
    </li>
  );
};

function isObject(x: unknown): x is Record<string, unknown> {
  return typeof x === "object" && x !== null && !Array.isArray(x);
}
