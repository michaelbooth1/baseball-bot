import { useMemo, type FC } from "react";
import type { BetRow, SessionFile } from "../types";
import { fmtMoney, fmtPct, fmtInt } from "../api";

/**
 * Multi-engine per-date view (2026-05-26). Renders ALL engines that
 * ran on a given date, on one scrollable page:
 *
 *   1. Compact comparison header  — one row per engine with bets/W-L/P&L/ROI;
 *      clicking a row anchor-scrolls down to that engine's detail card.
 *   2. Per-engine detail sections — each carries the per-engine summary
 *      metrics + the full bets table for that engine's session.
 *
 * Replaces the "click each per-config sub-row to see them one at a time"
 * flow for multi-engine dates. The per-config sidebar sub-rows still
 * work; they pin the per-day panel to a single engine via the existing
 * SessionsViewBody. This view only renders when a multi-engine date is
 * selected AND no per-config session is pinned.
 */
export type MultiEngineDayViewProps = {
  date: string;
  /** Sessions filtered to the selected date, ordered for stable display
   *  (typically alphabetical by config_label or modeFolder). */
  sessions: SessionFile[];
  /** Optional per-session metadata (configLabel) keyed by modeFolder.
   *  Lets the view show "A_current" headers instead of "paper_A_current". */
  configLabelByModeFolder: Record<string, string | undefined>;
  modeFolderBySession: Map<SessionFile, string>;
};

/** Per-engine derived metrics. */
type EngineRow = {
  modeFolder: string;
  configLabel: string;
  session: SessionFile;
  bets: BetRow[];
  placed: number;
  settled: number;
  wins: number;
  losses: number;
  profit: number;
  staked: number;
  winRate: number | null;
  roi: number | null;
  meanAsk: number | null;
  meanFv: number | null;
  // 2026-05-26 normalization (computed locally; mirrors the aggregator).
  nUniqueGameLines: number;
  betsPerUniqueGameLine: number | null;
  profitPerSettledBet: number | null;
};

function computeRow(
  modeFolder: string,
  configLabel: string,
  session: SessionFile,
): EngineRow {
  const bets = session.bets ?? [];
  const wins = bets.filter((b) => b.won === true).length;
  const losses = bets.filter((b) => b.won === false).length;
  const settled = wins + losses;
  const summary = session.summary ?? {};
  // Prefer the engine's own summary totals when present; fall back to
  // bet-list-derived sums so a freshly-stopped session that hasn't
  // written its summary yet still renders.
  const profitFromSummary =
    typeof summary.total_profit === "number" ? summary.total_profit : null;
  const stakedFromSummary =
    typeof summary.total_staked === "number" ? summary.total_staked : null;
  const profit =
    profitFromSummary ??
    bets.reduce((acc, b) => acc + (typeof b.profit === "number" ? b.profit : 0), 0);
  const staked =
    stakedFromSummary ??
    bets.reduce((acc, b) => acc + (typeof b.stake === "number" ? b.stake : 0), 0);
  const winRate = settled > 0 ? wins / settled : null;
  const roi = staked > 0 ? profit / staked : null;
  const askVals = bets
    .map((b) => b.entry_ask)
    .filter((x): x is number => typeof x === "number");
  const fvVals = bets
    .map((b) => b.fair_value)
    .filter((x): x is number => typeof x === "number");
  const meanAsk = askVals.length > 0 ? askVals.reduce((a, b) => a + b, 0) / askVals.length : null;
  const meanFv = fvVals.length > 0 ? fvVals.reduce((a, b) => a + b, 0) / fvVals.length : null;
  // 2026-05-26: per-bet + cohort breadth metrics (mirrors aggregator).
  const uniqueGameLines = new Set<string>();
  for (const b of bets) {
    if (b.game_pk != null && b.line != null) {
      uniqueGameLines.add(`${b.game_pk}|${b.line}`);
    }
  }
  const nUniqueGameLines = uniqueGameLines.size;
  const betsPerUniqueGameLine =
    nUniqueGameLines > 0 ? bets.length / nUniqueGameLines : null;
  const profitPerSettledBet = settled > 0 ? profit / settled : null;
  return {
    modeFolder,
    configLabel,
    session,
    bets,
    placed: bets.length,
    settled,
    wins,
    losses,
    profit,
    staked,
    winRate,
    roi,
    meanAsk,
    meanFv,
    nUniqueGameLines,
    betsPerUniqueGameLine,
    profitPerSettledBet,
  };
}

export const MultiEngineDayView: FC<MultiEngineDayViewProps> = ({
  date,
  sessions,
  configLabelByModeFolder,
  modeFolderBySession,
}) => {
  const rows = useMemo<EngineRow[]>(() => {
    const out: EngineRow[] = [];
    for (const s of sessions) {
      const mf = modeFolderBySession.get(s) ?? s.mode ?? "unknown";
      const label = configLabelByModeFolder[mf] ?? mf;
      out.push(computeRow(mf, label, s));
    }
    // Sort by config label so A, B, C, ... read in order.
    out.sort((a, b) => a.configLabel.localeCompare(b.configLabel));
    return out;
  }, [sessions, configLabelByModeFolder, modeFolderBySession]);

  if (rows.length === 0) {
    return (
      <section className="card">
        <h2 className="card-title">Multi-engine day view</h2>
        <p className="empty-state">No sessions for {date}.</p>
      </section>
    );
  }

  // Best ROI + best P&L + best $/Bet for quick ranking glance.
  // $/Bet is the volume-independent quality metric -- F_no_dedup will
  // grind out raw P&L on volume alone; profit_per_settled_bet exposes
  // whether each individual bet is actually +EV.
  const bestRoiRow = rows
    .filter((r) => r.roi !== null)
    .sort((a, b) => (b.roi ?? -Infinity) - (a.roi ?? -Infinity))[0];
  const bestPnlRow = [...rows].sort((a, b) => b.profit - a.profit)[0];
  const bestPpbRow = rows
    .filter((r) => r.profitPerSettledBet !== null && r.settled >= 3)
    .sort(
      (a, b) =>
        (b.profitPerSettledBet ?? -Infinity) -
        (a.profitPerSettledBet ?? -Infinity),
    )[0];
  // Baseline for the on-the-fly volume index = A_current when present,
  // else first row alphabetically. Mirrors aggregator behavior.
  const baselineRow =
    rows.find((r) => r.configLabel === "A_current") ?? rows[0];

  return (
    <div className="multi-engine-day">
      <section className="card">
        <header className="card-header">
          <div>
            <h1 className="session-date">{date}</h1>
            <span className="mode-badge mode-badge-multi">
              multi-engine ({rows.length} configs)
            </span>
          </div>
          <div className="generated-at">
            click an engine row below to jump to its detail
          </div>
        </header>

        {(bestRoiRow || bestPnlRow || bestPpbRow) && (
          <div className="multi-engine-headline">
            {bestRoiRow && (
              <div className="multi-engine-headline-item">
                <span className="multi-engine-headline-label">Best ROI</span>
                <span className="multi-engine-headline-value">
                  <code>{bestRoiRow.configLabel}</code> ({fmtPct(bestRoiRow.roi)})
                </span>
              </div>
            )}
            {bestPpbRow && (
              <div className="multi-engine-headline-item">
                <span className="multi-engine-headline-label">
                  Best $/Bet (quality, vs volume)
                </span>
                <span className="multi-engine-headline-value">
                  <code>{bestPpbRow.configLabel}</code>{" "}
                  ({fmtMoney(bestPpbRow.profitPerSettledBet, { signed: true })})
                </span>
              </div>
            )}
            {bestPnlRow && (
              <div className="multi-engine-headline-item">
                <span className="multi-engine-headline-label">Best P&amp;L</span>
                <span className="multi-engine-headline-value">
                  <code>{bestPnlRow.configLabel}</code>{" "}
                  ({fmtMoney(bestPnlRow.profit, { signed: true })})
                </span>
              </div>
            )}
            {baselineRow && (
              <div className="multi-engine-headline-item">
                <span className="multi-engine-headline-label">
                  Volume baseline
                </span>
                <span className="multi-engine-headline-value">
                  <code>{baselineRow.configLabel}</code>
                </span>
              </div>
            )}
          </div>
        )}

        <h2 className="card-title">Per-engine comparison</h2>
        <div className="table-scroll">
          <table className="multi-engine-table">
            <thead>
              <tr>
                <th>Config</th>
                <th className="num">Bets</th>
                <th className="num">Settled</th>
                <th>W-L</th>
                <th className="num">WR</th>
                <th className="num">Staked</th>
                <th className="num">P&amp;L</th>
                <th className="num">ROI</th>
                <th className="num">$/Bet</th>
                <th className="num">Vol Idx</th>
                <th className="num">Bets/GL</th>
                <th className="num">Mean Ask</th>
                <th className="num">Mean FV</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => {
                const profitClass =
                  r.profit > 0
                    ? "metric-positive"
                    : r.profit < 0
                      ? "metric-negative"
                      : "metric-neutral";
                const ppbClass =
                  r.profitPerSettledBet != null && r.profitPerSettledBet > 0
                    ? "metric-positive"
                    : r.profitPerSettledBet != null && r.profitPerSettledBet < 0
                      ? "metric-negative"
                      : "metric-neutral";
                const volIdx =
                  baselineRow && baselineRow.placed > 0
                    ? r.placed / baselineRow.placed
                    : null;
                return (
                  <tr key={r.modeFolder}>
                    <td>
                      <a
                        href={`#engine-${r.modeFolder}`}
                        className="multi-engine-anchor"
                      >
                        <code>{r.configLabel}</code>
                      </a>
                    </td>
                    <td className="num">{fmtInt(r.placed)}</td>
                    <td className="num">{fmtInt(r.settled)}</td>
                    <td>
                      {r.wins}-{r.losses}
                    </td>
                    <td className="num">{fmtPct(r.winRate)}</td>
                    <td className="num">{fmtMoney(r.staked)}</td>
                    <td className={"num " + profitClass}>
                      {fmtMoney(r.profit, { signed: true })}
                    </td>
                    <td className="num">{fmtPct(r.roi)}</td>
                    <td className={"num " + ppbClass}>
                      {fmtMoney(r.profitPerSettledBet, { signed: true })}
                    </td>
                    <td className="num">
                      {volIdx != null && !Number.isNaN(volIdx)
                        ? `${volIdx.toFixed(2)}x`
                        : "—"}
                    </td>
                    <td className="num">
                      {r.betsPerUniqueGameLine != null &&
                      !Number.isNaN(r.betsPerUniqueGameLine)
                        ? r.betsPerUniqueGameLine.toFixed(2)
                        : "—"}
                    </td>
                    <td className="num">{fmtNumOrDash(r.meanAsk, 3)}</td>
                    <td className="num">{fmtNumOrDash(r.meanFv, 3)}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </section>

      {rows.map((r) => (
        <EngineDetailCard key={r.modeFolder} row={r} />
      ))}
    </div>
  );
};

const EngineDetailCard: FC<{ row: EngineRow }> = ({ row }) => {
  const profitClass =
    row.profit > 0
      ? "metric-positive"
      : row.profit < 0
        ? "metric-negative"
        : "metric-neutral";
  return (
    <section
      className="card multi-engine-detail"
      id={`engine-${row.modeFolder}`}
    >
      <header className="card-header">
        <div>
          <h2 className="session-date multi-engine-detail-title">
            <code>{row.configLabel}</code>
          </h2>
          <span
            className={"mode-badge mode-badge-" + (row.session.mode ?? "unknown")}
          >
            {row.session.mode ?? "unknown"}
          </span>
        </div>
        <div className="generated-at">
          <code>{row.modeFolder}</code>
        </div>
      </header>
      <div className="metric-row">
        <div className="metric">
          <div className="metric-label">Bets</div>
          <div className="metric-value">
            {fmtInt(row.placed)} placed / {fmtInt(row.settled)} settled
          </div>
        </div>
        <div className="metric">
          <div className="metric-label">W-L</div>
          <div className="metric-value">
            {row.wins}-{row.losses}{" "}
            <span className="metric-subtle">({fmtPct(row.winRate)})</span>
          </div>
        </div>
        <div className="metric">
          <div className="metric-label">P&amp;L</div>
          <div className={"metric-value " + profitClass}>
            {fmtMoney(row.profit, { signed: true })}
          </div>
        </div>
        <div className="metric">
          <div className="metric-label">ROI</div>
          <div className={"metric-value " + profitClass}>
            {fmtPct(row.roi)}
          </div>
        </div>
        <div className="metric">
          <div className="metric-label">Staked</div>
          <div className="metric-value">{fmtMoney(row.staked)}</div>
        </div>
      </div>

      {row.bets.length === 0 ? (
        <p className="empty-state">No bets placed in this engine's session.</p>
      ) : (
        <div className="table-scroll">
          <table className="bets-table">
            <thead>
              <tr>
                <th>Game</th>
                <th>Line</th>
                <th>Side</th>
                <th>Inn</th>
                <th className="num">Ask</th>
                <th className="num">FV</th>
                <th className="num">Edge</th>
                <th className="num">Stake</th>
                <th className="num">Final</th>
                <th>Won</th>
                <th className="num">Profit</th>
              </tr>
            </thead>
            <tbody>
              {row.bets.map((b, i) => (
                <BetTableRow key={b.bet_id ?? i} bet={b} />
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
};

const BetTableRow: FC<{ bet: BetRow }> = ({ bet }) => {
  const game =
    bet.game ??
    [bet.away_abbrev, bet.home_abbrev].filter(Boolean).join("@") ??
    "?";
  const profit = bet.profit;
  const profitClass =
    profit != null && profit > 0
      ? "metric-positive"
      : profit != null && profit < 0
        ? "metric-negative"
        : "metric-neutral";
  return (
    <tr>
      <td>{game || "?"}</td>
      <td>{bet.line ?? "—"}</td>
      <td>{bet.side ?? "—"}</td>
      <td>{bet.inning ?? "—"}</td>
      <td className="num">{fmtNumOrDash(bet.entry_ask, 3)}</td>
      <td className="num">{fmtNumOrDash(bet.fair_value, 3)}</td>
      <td className="num">{fmtNumOrDash(bet.edge, 3, true)}</td>
      <td className="num">{fmtMoney(bet.stake)}</td>
      <td className="num">{bet.final_total ?? "—"}</td>
      <td>
        {bet.won === true ? (
          <span className="badge badge-win">W</span>
        ) : bet.won === false ? (
          <span className="badge badge-loss">L</span>
        ) : (
          "—"
        )}
      </td>
      <td className={"num " + profitClass}>
        {fmtMoney(profit, { signed: true })}
      </td>
    </tr>
  );
};

function fmtNumOrDash(
  v: number | null | undefined,
  digits = 3,
  signed = false,
): string {
  if (v === null || v === undefined || Number.isNaN(v)) return "—";
  const sign = signed && v >= 0 ? "+" : "";
  return `${sign}${v.toFixed(digits)}`;
}
