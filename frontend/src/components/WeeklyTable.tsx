import type { FC } from "react";
import type { SessionFile } from "../types";
import { fmtMoney, fmtPct } from "../api";

type Props = {
  /**
   * All session files (both live + paper) loaded from
   * `/api/sessions/<modeFolder>/<date>`. We group by ISO week +
   * mode internally. Order doesn't matter.
   *
   * Sessions are preferred over daily_human_review artifacts here
   * because session JSONs exist for EVERY date the engine ran,
   * regardless of which mode's `--sessions-dir` the daily refresh
   * was run against.
   */
  sessions: SessionFile[];
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
 * Reads session files (both live + paper) directly rather than
 * daily_human_review artifacts. Reason: daily_human_review is
 * built per-date from ONE `--sessions-dir` so it can miss the
 * other mode's session even when both exist on disk. Session
 * JSONs always exist in their respective folders.
 *
 * Groups by ISO week (Monday -> Sunday). Each row sums:
 *   - sessions: count of session files in that week
 *   - count / wins / losses: per-bet from each session's bets[]
 *   - profit: from session.summary.total_profit (per-session
 *     pre-summed by the engine)
 *   - stake: from session.summary.total_staked (authoritative
 *     per-session aggregate)
 *   - win_rate = wins / (wins + losses)
 *   - roi = profit / stake
 *
 * Per-mode subtotal rows surface when more than one distinct mode
 * appears in the loaded window, so live P&L and paper P&L can be
 * read separately rather than mixed into one (potentially
 * misleading) ROI.
 *
 * Mixed-mode caveat fires when paper + live both appear: paper's
 * 100% taker assumption overstates realizable P&L.
 */
export const WeeklyTable: FC<Props> = ({ sessions }) => {
  // Group sessions by week-start (Monday)
  const byWeek = new Map<string, SessionFile[]>();
  for (const s of sessions) {
    const d = sessionDate(s);
    if (!d) continue;
    const weekStart = mondayOfWeek(d);
    if (!byWeek.has(weekStart)) byWeek.set(weekStart, []);
    byWeek.get(weekStart)!.push(s);
  }

  const weekRows: WeekRow[] = Array.from(byWeek.entries())
    .map(([weekStart, weekSessions]) => {
      const bets = weekSessions.flatMap((s) => s.bets ?? []);
      const wins = bets.filter((b) => b.won === true).length;
      const losses = bets.filter((b) => b.won === false).length;
      const decided = wins + losses;
      const stake = weekSessions.reduce(
        (sum, s) => sum + (sessionTotalStaked(s) ?? 0),
        0,
      );
      const profit = weekSessions.reduce(
        (sum, s) => sum + (sessionTotalProfit(s) ?? 0),
        0,
      );
      const modeCounts: Record<string, number> = {};
      for (const s of weekSessions) {
        const m = s.mode ?? "unknown";
        modeCounts[m] = (modeCounts[m] ?? 0) + 1;
      }
      return {
        weekStart,
        weekEnd: addDays(weekStart, 6),
        sessions: weekSessions.length,
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
    .sort((a, b) => b.weekStart.localeCompare(a.weekStart));

  // Per-mode subtotal rows
  const byMode = new Map<string, SessionFile[]>();
  for (const s of sessions) {
    const m = s.mode ?? "unknown";
    if (!byMode.has(m)) byMode.set(m, []);
    byMode.get(m)!.push(s);
  }
  const modeRows: ModeAggregate[] = Array.from(byMode.entries())
    .map(([mode, modeSessions]) => {
      const bets = modeSessions.flatMap((s) => s.bets ?? []);
      const wins = bets.filter((b) => b.won === true).length;
      const losses = bets.filter((b) => b.won === false).length;
      const decided = wins + losses;
      const stake = modeSessions.reduce(
        (sum, s) => sum + (sessionTotalStaked(s) ?? 0),
        0,
      );
      const profit = modeSessions.reduce(
        (sum, s) => sum + (sessionTotalProfit(s) ?? 0),
        0,
      );
      return {
        mode,
        sessions: modeSessions.length,
        count: bets.length,
        wins,
        losses,
        winRate: decided > 0 ? wins / decided : null,
        profit,
        stake,
        roi: stake > 0 ? profit / stake : null,
      };
    })
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
          No session data on disk. (Loaded {sessions.length} session
          {sessions.length === 1 ? "" : "s"}.)
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
  const showModeBreakdown = modeRows.length > 1;
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
        Bets grouped by ISO week (Monday → Sunday) from session JSONs
        (paper + live). Win-rate denominator excludes unsettled bets;
        stake includes them.
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
  const modesDisplay = Object.entries(w.modeCounts)
    .sort(([a], [b]) => {
      const rank = (m: string) =>
        m === "live" ? 0 : m === "paper" ? 1 : 2;
      const r = rank(a) - rank(b);
      return r !== 0 ? r : a.localeCompare(b);
    })
    .map(([m, c]) =>
      Object.keys(w.modeCounts).length === 1 ? m : `${m} (${c})`,
    )
    .join(", ");
  return (
    <tr>
      <td>
        {w.weekStart} → {w.weekEnd}
      </td>
      <td className="modes-cell">{modesDisplay}</td>
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

/** Session date lives at `.date` in the session JSON. */
function sessionDate(s: SessionFile): string | null {
  return typeof s.date === "string" && s.date ? s.date : null;
}

function sessionTotalStaked(s: SessionFile): number | null {
  const v = s.summary?.total_staked;
  return typeof v === "number" ? v : null;
}

function sessionTotalProfit(s: SessionFile): number | null {
  const v = s.summary?.total_profit;
  return typeof v === "number" ? v : null;
}

/** YYYY-MM-DD of the Monday of the ISO week containing `date`. */
function mondayOfWeek(date: string): string {
  const d = new Date(date + "T00:00:00Z");
  const dayOfWeek = d.getUTCDay();
  const daysFromMonday = (dayOfWeek + 6) % 7;
  d.setUTCDate(d.getUTCDate() - daysFromMonday);
  return d.toISOString().slice(0, 10);
}

function addDays(date: string, days: number): string {
  const d = new Date(date + "T00:00:00Z");
  d.setUTCDate(d.getUTCDate() + days);
  return d.toISOString().slice(0, 10);
}
