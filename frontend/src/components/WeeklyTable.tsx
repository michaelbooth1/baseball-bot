import type { FC } from "react";
import type { DailyReview } from "../types";
import { fmtMoney, fmtPct } from "../api";

type Props = {
  /** Trailing N reviews, any order. We group + sort internally. */
  reviews: DailyReview[];
};

type WeekRow = {
  weekStart: string; // YYYY-MM-DD of the Monday
  weekEnd: string;   // YYYY-MM-DD of the Sunday
  sessions: number;
  count: number;
  wins: number;
  losses: number;
  winRate: number | null;
  profit: number;
  stake: number;
  roi: number | null;
  /** Per-mode session counts within the week, e.g. {live: 5, paper: 2}. */
  modeCounts: Record<string, number>;
};

type ModeAggregate = {
  mode: string;
  sessions: number;
  count: number;
  wins: number;
  losses: number;
  winRate: number | null;
  profit: number;
  stake: number;
  roi: number | null;
};

/**
 * Weekly win/loss/ROI roll-up.
 *
 * Groups loaded daily-review bets by ISO week (Monday -> Sunday).
 * Each row sums:
 *   - sessions: count of daily reviews in that week
 *   - count / wins / losses: per-bet from each review's `bets[].won`
 *   - profit: from `bet_totals.profit` (per-session pre-summed by
 *     the daily-review builder; reliable across paper + live modes
 *     where per-bet `stake` is missing)
 *   - stake: from `session_summary.total_staked` (authoritative
 *     per-session aggregate; per-bet `stake` is null in the daily
 *     review's compact bet rows)
 *   - win_rate = wins / (wins + losses)
 *   - roi = profit / stake
 *
 * The footer `Total` row sums all loaded weeks. ISO week convention
 * (Mon-Sun) matches the project's `weekly_drift_rollup` artifact.
 */
export const WeeklyTable: FC<Props> = ({ reviews }) => {
  // Group reviews by week-start (Monday)
  const byWeek = new Map<string, DailyReview[]>();
  for (const r of reviews) {
    if (!r.session_date) continue;
    const weekStart = mondayOfWeek(r.session_date);
    if (!byWeek.has(weekStart)) byWeek.set(weekStart, []);
    byWeek.get(weekStart)!.push(r);
  }

  const weekRows: WeekRow[] = Array.from(byWeek.entries())
    .map(([weekStart, weekReviews]) => {
      const bets = weekReviews.flatMap((r) => r.bets ?? []);
      const wins = bets.filter((b) => b.won === true).length;
      const losses = bets.filter((b) => b.won === false).length;
      const decided = wins + losses;
      // Stake + profit live in the per-session summary blocks
      // (the per-bet `stake` field is null in the compact daily-
      // review bet rows; total_staked + bet_totals.profit are
      // pre-aggregated by build_daily_human_review_report.py).
      const stake = weekReviews.reduce(
        (s, r) => s + (sessionTotalStaked(r) ?? 0),
        0,
      );
      const profit = weekReviews.reduce(
        (s, r) => s + (sessionTotalProfit(r) ?? 0),
        0,
      );
      const modeCounts: Record<string, number> = {};
      for (const r of weekReviews) {
        const m = r.mode ?? "unknown";
        modeCounts[m] = (modeCounts[m] ?? 0) + 1;
      }
      return {
        weekStart,
        weekEnd: addDays(weekStart, 6),
        sessions: weekReviews.length,
        count: bets.length,
        wins,
        losses,
        winRate: decided > 0 ? wins / decided : null,
        profit,
        stake,
        roi: stake > 0 ? profit / stake : null,
        modeCounts,
      };
    })
    // Newest week first
    .sort((a, b) => b.weekStart.localeCompare(a.weekStart));

  // Per-mode subtotal rows -- so the operator can read live and
  // paper P&L separately rather than mixed into one ROI.
  const byMode = new Map<string, DailyReview[]>();
  for (const r of reviews) {
    const m = r.mode ?? "unknown";
    if (!byMode.has(m)) byMode.set(m, []);
    byMode.get(m)!.push(r);
  }
  const modeRows: ModeAggregate[] = Array.from(byMode.entries())
    .map(([mode, modeReviews]) => {
      const bets = modeReviews.flatMap((r) => r.bets ?? []);
      const wins = bets.filter((b) => b.won === true).length;
      const losses = bets.filter((b) => b.won === false).length;
      const decided = wins + losses;
      const stake = modeReviews.reduce(
        (s, r) => s + (sessionTotalStaked(r) ?? 0),
        0,
      );
      const profit = modeReviews.reduce(
        (s, r) => s + (sessionTotalProfit(r) ?? 0),
        0,
      );
      return {
        mode,
        sessions: modeReviews.length,
        count: bets.length,
        wins,
        losses,
        winRate: decided > 0 ? wins / decided : null,
        profit,
        stake,
        roi: stake > 0 ? profit / stake : null,
      };
    })
    // Sort: live -> paper -> rest alphabetical. Live is the
    // operationally important one so it leads.
    .sort((a, b) => {
      const rank = (m: string) =>
        m === "live" ? 0 : m === "paper" ? 1 : 2;
      const r = rank(a.mode) - rank(b.mode);
      return r !== 0 ? r : a.mode.localeCompare(b.mode);
    });

  if (weekRows.length === 0) {
    return (
      <section className="card">
        <h2 className="card-title">Weekly results</h2>
        <p className="empty-state">
          No bet data in the trailing window. (Loaded {reviews.length}{" "}
          review{reviews.length === 1 ? "" : "s"}.)
        </p>
      </section>
    );
  }

  // Footer totals
  const totalSessions = weekRows.reduce((s, w) => s + w.sessions, 0);
  const totalCount = weekRows.reduce((s, w) => s + w.count, 0);
  const totalWins = weekRows.reduce((s, w) => s + w.wins, 0);
  const totalLosses = weekRows.reduce((s, w) => s + w.losses, 0);
  const totalDecided = totalWins + totalLosses;
  const totalProfit = weekRows.reduce((s, w) => s + w.profit, 0);
  const totalStake = weekRows.reduce((s, w) => s + w.stake, 0);
  const totalRoi = totalStake > 0 ? totalProfit / totalStake : null;
  const totalWinRate = totalDecided > 0 ? totalWins / totalDecided : null;

  // Whether to render per-mode subtotals: only when more than one
  // distinct mode appears in the loaded reviews. Single-mode windows
  // would just duplicate the grand-total row.
  const showModeBreakdown = modeRows.length > 1;
  // Mixed-mode caveat: surface only when paper sessions are mixed
  // with live sessions, since that's the case where summing the ROI
  // is misleading (paper assumes 100% taker; live has ~46% fill).
  const hasPaper = modeRows.some((m) => m.mode === "paper");
  const hasLive = modeRows.some((m) => m.mode === "live");
  const showPaperCaveat = hasPaper && hasLive;

  return (
    <section className="card">
      <h2 className="card-title">
        Weekly results ({weekRows.length} week
        {weekRows.length === 1 ? "" : "s"})
      </h2>
      <p className="card-meta">
        Bets grouped by ISO week (Monday → Sunday). Each row sums
        per-bet outcomes across that week's sessions. Win-rate
        denominator excludes unsettled bets; stake includes them.
        {showPaperCaveat && (
          <>
            {" "}
            <strong>Paper + live mixed in this window:</strong> paper
            mode assumes 100% taker fill at entry_ask while live has
            ~46% fill rate with adverse selection, so paper P&amp;L
            overstates realizable returns. Read the per-mode subtotal
            rows below for an apples-to-apples view.
          </>
        )}
      </p>
      <div className="table-scroll">
        <table className="bets-table">
          <thead>
            <tr>
              <th>Week</th>
              <th>Modes</th>
              <th className="num">Sessions</th>
              <th className="num">Bets</th>
              <th className="num">W</th>
              <th className="num">L</th>
              <th className="num">Win rate</th>
              <th className="num">Stake</th>
              <th className="num">Profit</th>
              <th className="num">ROI</th>
            </tr>
          </thead>
          <tbody>
            {weekRows.map((w) => (
              <WeekTableRow key={w.weekStart} week={w} />
            ))}
            {showModeBreakdown &&
              modeRows.map((m) => (
                <tr key={`mode-${m.mode}`} className="subtotal-row">
                  <td>
                    By mode: <strong>{m.mode}</strong>
                  </td>
                  <td>—</td>
                  <td className="num">{m.sessions}</td>
                  <td className="num">{m.count}</td>
                  <td className="num">{m.wins}</td>
                  <td className="num">{m.losses}</td>
                  <td className="num">{fmtPct(m.winRate)}</td>
                  <td className="num">{fmtMoney(m.stake)}</td>
                  <td className={"num " + signClass(m.profit)}>
                    {fmtMoney(m.profit, { signed: true })}
                  </td>
                  <td className={"num " + signClass(m.roi)}>
                    {fmtPct(m.roi)}
                  </td>
                </tr>
              ))}
            <tr className="total-row">
              <td>
                <strong>Total</strong>
              </td>
              <td>—</td>
              <td className="num">
                <strong>{totalSessions}</strong>
              </td>
              <td className="num">
                <strong>{totalCount}</strong>
              </td>
              <td className="num">
                <strong>{totalWins}</strong>
              </td>
              <td className="num">
                <strong>{totalLosses}</strong>
              </td>
              <td className="num">
                <strong>{fmtPct(totalWinRate)}</strong>
              </td>
              <td className="num">
                <strong>{fmtMoney(totalStake)}</strong>
              </td>
              <td className={"num " + signClass(totalProfit)}>
                <strong>{fmtMoney(totalProfit, { signed: true })}</strong>
              </td>
              <td className={"num " + signClass(totalRoi)}>
                <strong>{fmtPct(totalRoi)}</strong>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </section>
  );
};

const WeekTableRow: FC<{ week: WeekRow }> = ({ week: w }) => {
  return (
    <tr>
      <td>
        {w.weekStart} → {w.weekEnd}
      </td>
      <td className="num">{w.sessions}</td>
      <td className="num">{w.count}</td>
      <td className="num">{w.wins}</td>
      <td className="num">{w.losses}</td>
      <td className="num">{fmtPct(w.winRate)}</td>
      <td className="num">{fmtMoney(w.stake)}</td>
      <td className={"num " + signClass(w.profit)}>
        {fmtMoney(w.profit, { signed: true })}
      </td>
      <td className={"num " + signClass(w.roi)}>{fmtPct(w.roi)}</td>
    </tr>
  );
};

function signClass(v: number | null | undefined): string {
  if (v === null || v === undefined || Number.isNaN(v)) return "metric-neutral";
  if (v > 0) return "metric-positive";
  if (v < 0) return "metric-negative";
  return "metric-neutral";
}

/**
 * Authoritative per-session stake. Pulled from `session_summary
 * .total_staked` which is pre-aggregated by the daily-review
 * builder across paper + live modes. Per-bet `bet.stake` in the
 * compact bet rows is null; don't try to derive from that.
 */
function sessionTotalStaked(r: DailyReview): number | null {
  const v = (r.session_summary as Record<string, unknown> | undefined)?.[
    "total_staked"
  ];
  return typeof v === "number" ? v : null;
}

/**
 * Authoritative per-session profit. `bet_totals.profit` is the
 * builder's per-session sum. Falls back to
 * `session_summary.total_profit` for older review formats.
 */
function sessionTotalProfit(r: DailyReview): number | null {
  const fromTotals = r.bet_totals?.profit;
  if (typeof fromTotals === "number") return fromTotals;
  const fromSummary = r.session_summary?.total_profit;
  return typeof fromSummary === "number" ? fromSummary : null;
}

/** YYYY-MM-DD of the Monday of the ISO week containing `date`. */
function mondayOfWeek(date: string): string {
  const d = new Date(date + "T00:00:00Z");
  const dayOfWeek = d.getUTCDay(); // 0 = Sunday, 1 = Monday, ..., 6 = Saturday
  // Distance from previous Monday: Sun -> 6, Mon -> 0, Tue -> 1, ...
  const daysFromMonday = (dayOfWeek + 6) % 7;
  d.setUTCDate(d.getUTCDate() - daysFromMonday);
  return d.toISOString().slice(0, 10);
}

function addDays(date: string, days: number): string {
  const d = new Date(date + "T00:00:00Z");
  d.setUTCDate(d.getUTCDate() + days);
  return d.toISOString().slice(0, 10);
}
