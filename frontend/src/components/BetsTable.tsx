import type { FC } from "react";
import type { BetRow, DailyReview } from "../types";
import { fmtMoney } from "../api";

type Props = {
  review: DailyReview;
};

/**
 * Per-bet table. Each row is one filled / placed bet from the
 * session JSON, with the most operationally relevant columns first:
 * game, line/side, ask, FV, edge, won?, profit.
 *
 * Alt-A shadow columns (`inferred_state_base_empirical`,
 * `fair_value_alt_empirical`) surface when present so the operator
 * can spot per-bet Alt-A delta without leaving the daily review.
 */
export const BetsTable: FC<Props> = ({ review }) => {
  const bets = review.bets ?? [];
  if (bets.length === 0) {
    return (
      <section className="card">
        <h2 className="card-title">Bets</h2>
        <p className="empty-state">No bets placed in this session.</p>
      </section>
    );
  }
  return (
    <section className="card">
      <h2 className="card-title">Bets ({bets.length})</h2>
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
              <th className="num">Empirical</th>
              <th className="num">Final</th>
              <th>Won</th>
              <th className="num">Profit</th>
            </tr>
          </thead>
          <tbody>
            {bets.map((b, i) => (
              <BetTableRow key={b.bet_id ?? i} bet={b} />
            ))}
          </tbody>
        </table>
      </div>
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
      <td className="num">{fmtNum(bet.entry_ask, 3)}</td>
      <td className="num">{fmtNum(bet.fair_value, 3)}</td>
      <td className="num">{fmtNum(bet.edge, 3, true)}</td>
      <td className="num">{fmtNum(bet.inferred_state_base_empirical, 3)}</td>
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

function fmtNum(v: number | null | undefined, digits = 3, signed = false): string {
  if (v === null || v === undefined || Number.isNaN(v)) return "—";
  const sign = signed && v >= 0 ? "+" : "";
  return `${sign}${v.toFixed(digits)}`;
}
