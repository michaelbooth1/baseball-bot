import type { FC } from "react";
import type { DailyReview } from "../types";
import { fmtMoney, fmtPct, fmtInt } from "../api";

type Props = {
  review: DailyReview;
};

/** Top-of-page session summary card. Mode badge + W-L + profit/ROI. */
export const SessionSummary: FC<Props> = ({ review }) => {
  const summary = review.session_summary ?? {};
  const totals = review.bet_totals ?? {};
  const placed = summary.orders_placed ?? totals.count ?? 0;
  const filled = summary.orders_filled ?? totals.filled ?? 0;
  const wins = summary.wins ?? totals.wins ?? 0;
  const losses = summary.losses ?? totals.losses ?? 0;
  const profit = summary.total_profit ?? totals.profit ?? 0;
  const roi = summary.roi ?? totals.roi;

  const profitClass =
    profit > 0 ? "metric-positive" : profit < 0 ? "metric-negative" : "metric-neutral";

  return (
    <section className="card">
      <header className="card-header">
        <div>
          <h1 className="session-date">{review.session_date ?? "?"}</h1>
          <span className={"mode-badge mode-badge-" + (review.mode ?? "unknown")}>
            {review.mode ?? "unknown"}
          </span>
        </div>
        <div className="generated-at">
          generated {review.generated_at_utc ?? "?"}
        </div>
      </header>
      <div className="metric-row">
        <div className="metric">
          <div className="metric-label">Bets</div>
          <div className="metric-value">
            {fmtInt(placed)} placed / {fmtInt(filled)} filled
          </div>
        </div>
        <div className="metric">
          <div className="metric-label">Result</div>
          <div className="metric-value">
            {fmtInt(wins)}-{fmtInt(losses)}
          </div>
        </div>
        <div className="metric">
          <div className="metric-label">Profit</div>
          <div className={"metric-value " + profitClass}>
            {fmtMoney(profit, { signed: true })}
          </div>
        </div>
        <div className="metric">
          <div className="metric-label">ROI</div>
          <div className={"metric-value " + profitClass}>{fmtPct(roi)}</div>
        </div>
        <div className="metric">
          <div className="metric-label">Win rate</div>
          <div className="metric-value">{fmtPct(totals.win_rate)}</div>
        </div>
      </div>
    </section>
  );
};
