import { useMemo, useState, type FC } from "react";
import { fmtInt, fmtMoney, fmtPct } from "../api";
import type { BetRow, SessionFile } from "../types";

/**
 * 2026-05-30: cross-date per-model aggregate table for the main
 * comparison page. Loads bets from EVERY available session for each
 * configLabel and rolls them up into a single row per model so the
 * operator can read "model X has placed N bets all-time, W-L, ROI,
 * $/Bet, avg ask, avg edge, max drawdown" without clicking through
 * individual range reports.
 *
 * - Filter: side (All / Over / Under). Bet-level filter; aggregates
 *   recompute when the side changes.
 * - Sortable: click any column header to sort ascending; click again
 *   to descend.
 * - Drilldown: click a model row to expand a per-bet table beneath.
 *
 * No backend changes — uses sessions already loaded by App.tsx via
 * `/api/sessions/{modeFolder}/{date}`. The model label key is the
 * session's `_configLabel` (set by App.tsx at load time from the
 * session index entry). Sessions without `_configLabel` are grouped
 * under `(legacy)` so a single-engine paper or live history still
 * surfaces.
 */

type SideFilter = "all" | "over" | "under";

type AggregateRow = {
  configLabel: string;
  nBets: number;
  nSettled: number;
  nWon: number;
  nLost: number;
  totalStake: number;
  totalProfit: number;
  meanAsk: number | null;
  meanFv: number | null;
  meanEdge: number | null;
  maxDrawdown: number;
  daysActive: number;
  firstDate: string | null;
  lastDate: string | null;
};

type SortKey =
  | "configLabel"
  | "nBets"
  | "nSettled"
  | "wl"
  | "winRate"
  | "totalProfit"
  | "roi"
  | "profitPerBet"
  | "meanAsk"
  | "meanFv"
  | "meanEdge"
  | "maxDrawdown"
  | "daysActive";

type SortDir = "asc" | "desc";

const LEGACY_KEY = "(legacy)";

/** Collect per-config rows from every loaded session. Each bet on
 *  every session contributes to exactly one row, keyed by
 *  `_configLabel` (or `LEGACY_KEY` when absent). */
function aggregate(
  sessions: SessionFile[],
  sideFilter: SideFilter,
): AggregateRow[] {
  type Acc = {
    configLabel: string;
    bets: BetRow[];
    dates: Set<string>;
  };
  const byConfig = new Map<string, Acc>();
  for (const s of sessions) {
    const key = s._configLabel || LEGACY_KEY;
    let acc = byConfig.get(key);
    if (!acc) {
      acc = { configLabel: key, bets: [], dates: new Set() };
      byConfig.set(key, acc);
    }
    if (s.date) acc.dates.add(s.date);
    for (const b of s.bets ?? []) {
      if (sideFilter !== "all") {
        const side = (b.side || "").toLowerCase();
        if (side !== sideFilter) continue;
      }
      acc.bets.push(b);
    }
  }

  const rows: AggregateRow[] = [];
  for (const acc of byConfig.values()) {
    // Sort settled bets by settled_at (or placed_at as fallback) so
    // running-profit drawdown is computed in chronological order
    // across sessions.
    const settledBets = acc.bets
      .filter((b) => b.settled === true || b.won != null)
      .slice()
      .sort((a, b) => {
        const ta = a.settled_at || a.placed_at || "";
        const tb = b.settled_at || b.placed_at || "";
        return ta.localeCompare(tb);
      });
    let nWon = 0;
    let nLost = 0;
    let totalProfit = 0;
    let runningProfit = 0;
    let runningPeak = 0;
    let maxDrawdown = 0;
    for (const b of settledBets) {
      const p = typeof b.profit === "number" ? b.profit : 0;
      totalProfit += p;
      runningProfit += p;
      if (runningProfit > runningPeak) runningPeak = runningProfit;
      const dd = runningProfit - runningPeak;
      if (dd < maxDrawdown) maxDrawdown = dd;
      if (b.won === true) nWon += 1;
      else if (b.won === false) nLost += 1;
    }
    let totalStake = 0;
    let askSum = 0;
    let askN = 0;
    let fvSum = 0;
    let fvN = 0;
    let edgeSum = 0;
    let edgeN = 0;
    for (const b of acc.bets) {
      if (typeof b.stake === "number") totalStake += b.stake;
      if (typeof b.entry_ask === "number") {
        askSum += b.entry_ask;
        askN += 1;
      }
      if (typeof b.fair_value === "number") {
        fvSum += b.fair_value;
        fvN += 1;
      }
      if (typeof b.edge === "number") {
        edgeSum += b.edge;
        edgeN += 1;
      }
    }
    const dates = Array.from(acc.dates).sort();
    rows.push({
      configLabel: acc.configLabel,
      nBets: acc.bets.length,
      nSettled: settledBets.length,
      nWon,
      nLost,
      totalStake,
      totalProfit,
      meanAsk: askN > 0 ? askSum / askN : null,
      meanFv: fvN > 0 ? fvSum / fvN : null,
      meanEdge: edgeN > 0 ? edgeSum / edgeN : null,
      maxDrawdown,
      daysActive: acc.dates.size,
      firstDate: dates[0] ?? null,
      lastDate: dates[dates.length - 1] ?? null,
    });
  }
  return rows;
}

function rowSortValue(r: AggregateRow, key: SortKey): number | string {
  switch (key) {
    case "configLabel":
      return r.configLabel;
    case "nBets":
      return r.nBets;
    case "nSettled":
      return r.nSettled;
    case "wl":
      return r.nWon - r.nLost;
    case "winRate": {
      const decided = r.nWon + r.nLost;
      return decided > 0 ? r.nWon / decided : -1; // pin "no data" to bottom in asc
    }
    case "totalProfit":
      return r.totalProfit;
    case "roi":
      return r.totalStake > 0 ? r.totalProfit / r.totalStake : -1e9;
    case "profitPerBet":
      return r.nSettled > 0 ? r.totalProfit / r.nSettled : -1e9;
    case "meanAsk":
      return r.meanAsk ?? -1;
    case "meanFv":
      return r.meanFv ?? -1;
    case "meanEdge":
      return r.meanEdge ?? -1;
    case "maxDrawdown":
      return r.maxDrawdown; // already negative or 0
    case "daysActive":
      return r.daysActive;
  }
}

type Props = {
  sessions: SessionFile[];
};

export const AllSessionsBreakdown: FC<Props> = ({ sessions }) => {
  const [side, setSide] = useState<SideFilter>("all");
  const [sortKey, setSortKey] = useState<SortKey>("totalProfit");
  const [sortDir, setSortDir] = useState<SortDir>("desc");
  const [expanded, setExpanded] = useState<string | null>(null);

  const rows = useMemo(() => aggregate(sessions, side), [sessions, side]);

  const sorted = useMemo(() => {
    const copy = rows.slice();
    copy.sort((a, b) => {
      const va = rowSortValue(a, sortKey);
      const vb = rowSortValue(b, sortKey);
      let cmp: number;
      if (typeof va === "string" || typeof vb === "string") {
        cmp = String(va).localeCompare(String(vb));
      } else {
        cmp = (va as number) - (vb as number);
      }
      return sortDir === "asc" ? cmp : -cmp;
    });
    return copy;
  }, [rows, sortKey, sortDir]);

  const totals = useMemo(() => {
    let bets = 0;
    let settled = 0;
    let won = 0;
    let lost = 0;
    let stake = 0;
    let profit = 0;
    for (const r of rows) {
      bets += r.nBets;
      settled += r.nSettled;
      won += r.nWon;
      lost += r.nLost;
      stake += r.totalStake;
      profit += r.totalProfit;
    }
    const decided = won + lost;
    return {
      bets,
      settled,
      won,
      lost,
      stake,
      profit,
      winRate: decided > 0 ? won / decided : null,
      roi: stake > 0 ? profit / stake : null,
    };
  }, [rows]);

  const onHeaderClick = (key: SortKey) => {
    if (sortKey === key) {
      setSortDir((d) => (d === "asc" ? "desc" : "asc"));
    } else {
      setSortKey(key);
      // Sensible defaults: text fields ascend, numeric descend.
      setSortDir(key === "configLabel" ? "asc" : "desc");
    }
  };

  const sortIndicator = (key: SortKey) =>
    sortKey === key ? (sortDir === "asc" ? " ▲" : " ▼") : "";

  if (sessions.length === 0) {
    return (
      <section className="compare-section">
        <h3>All sessions — per-model totals</h3>
        <p className="compare-empty">No sessions loaded yet.</p>
      </section>
    );
  }

  return (
    <section className="compare-section">
      <h3>
        All sessions — per-model totals
        <span className="compare-baseline-chip">
          across <code>{rows.reduce((n, r) => n + r.daysActive, 0)}</code>{" "}
          model-days
        </span>
      </h3>
      <div className="compare-allsessions-controls">
        <div className="compare-allsessions-filter">
          <label htmlFor="side-filter">Side:</label>
          <select
            id="side-filter"
            value={side}
            onChange={(e) => setSide(e.target.value as SideFilter)}
          >
            <option value="all">All</option>
            <option value="over">Over</option>
            <option value="under">Under</option>
          </select>
        </div>
        <div className="compare-allsessions-totals">
          fleet total: <strong>{fmtInt(totals.bets)}</strong> bets,{" "}
          <strong>
            {totals.won}-{totals.lost}
          </strong>{" "}
          settled, WR <strong>{fmtPct(totals.winRate)}</strong>, P&amp;L{" "}
          <strong>{fmtMoney(totals.profit, { signed: true })}</strong>, ROI{" "}
          <strong>{fmtPct(totals.roi)}</strong>
        </div>
      </div>
      <div className="compare-table-scroll">
        <table className="compare-table compare-allsessions-table">
          <thead>
            <tr>
              <th />
              <SortableHeader
                label="Model"
                colKey="configLabel"
                onClick={onHeaderClick}
                indicator={sortIndicator}
              />
              <SortableHeader
                label="Bets"
                colKey="nBets"
                onClick={onHeaderClick}
                indicator={sortIndicator}
              />
              <SortableHeader
                label="Settled"
                colKey="nSettled"
                onClick={onHeaderClick}
                indicator={sortIndicator}
              />
              <SortableHeader
                label="W-L"
                colKey="wl"
                onClick={onHeaderClick}
                indicator={sortIndicator}
              />
              <SortableHeader
                label="WR"
                colKey="winRate"
                onClick={onHeaderClick}
                indicator={sortIndicator}
              />
              <SortableHeader
                label="P&L"
                colKey="totalProfit"
                onClick={onHeaderClick}
                indicator={sortIndicator}
              />
              <SortableHeader
                label="ROI"
                colKey="roi"
                onClick={onHeaderClick}
                indicator={sortIndicator}
              />
              <SortableHeader
                label="$/Bet"
                colKey="profitPerBet"
                onClick={onHeaderClick}
                indicator={sortIndicator}
              />
              <SortableHeader
                label="Avg Ask"
                colKey="meanAsk"
                onClick={onHeaderClick}
                indicator={sortIndicator}
              />
              <SortableHeader
                label="Avg FV"
                colKey="meanFv"
                onClick={onHeaderClick}
                indicator={sortIndicator}
              />
              <SortableHeader
                label="Avg Edge"
                colKey="meanEdge"
                onClick={onHeaderClick}
                indicator={sortIndicator}
              />
              <SortableHeader
                label="Max DD"
                colKey="maxDrawdown"
                onClick={onHeaderClick}
                indicator={sortIndicator}
              />
              <SortableHeader
                label="Days"
                colKey="daysActive"
                onClick={onHeaderClick}
                indicator={sortIndicator}
              />
              <th>Window</th>
            </tr>
          </thead>
          <tbody>
            {sorted.map((r) => {
              const decided = r.nWon + r.nLost;
              const winRate = decided > 0 ? r.nWon / decided : null;
              const roi = r.totalStake > 0 ? r.totalProfit / r.totalStake : null;
              const profitPerBet =
                r.nSettled > 0 ? r.totalProfit / r.nSettled : null;
              const isExpanded = expanded === r.configLabel;
              return (
                <RowAndDrilldown
                  key={r.configLabel}
                  row={r}
                  isExpanded={isExpanded}
                  onToggle={() =>
                    setExpanded(isExpanded ? null : r.configLabel)
                  }
                  winRate={winRate}
                  roi={roi}
                  profitPerBet={profitPerBet}
                  sessions={sessions}
                  side={side}
                />
              );
            })}
          </tbody>
        </table>
      </div>
      <p className="compare-disagreement-summary">
        Click a model row to expand the per-bet drilldown.{" "}
        <strong>Max DD</strong> is the deepest dip below the running profit
        peak (settled-bet chronological order across all sessions).{" "}
        <strong>$/Bet</strong> = total P&amp;L / settled bets, side-filtered.
      </p>
    </section>
  );
};

const SortableHeader: FC<{
  label: string;
  colKey: SortKey;
  onClick: (k: SortKey) => void;
  indicator: (k: SortKey) => string;
}> = ({ label, colKey, onClick, indicator }) => (
  <th
    className="compare-allsessions-sortable"
    onClick={() => onClick(colKey)}
    role="button"
  >
    {label}
    {indicator(colKey)}
  </th>
);

const RowAndDrilldown: FC<{
  row: AggregateRow;
  isExpanded: boolean;
  onToggle: () => void;
  winRate: number | null;
  roi: number | null;
  profitPerBet: number | null;
  sessions: SessionFile[];
  side: SideFilter;
}> = ({
  row,
  isExpanded,
  onToggle,
  winRate,
  roi,
  profitPerBet,
  sessions,
  side,
}) => {
  // Collect this model's bets across all sessions (and apply side
  // filter). Sorted by placed_at desc so the drilldown opens with the
  // most recent bets first.
  const bets = useMemo(() => {
    const out: Array<BetRow & { _date?: string }> = [];
    for (const s of sessions) {
      if ((s._configLabel || LEGACY_KEY) !== row.configLabel) continue;
      for (const b of s.bets ?? []) {
        if (side !== "all") {
          const sd = (b.side || "").toLowerCase();
          if (sd !== side) continue;
        }
        out.push({ ...b, _date: s.date });
      }
    }
    out.sort((a, b) => {
      const ta = a.placed_at || a.settled_at || "";
      const tb = b.placed_at || b.settled_at || "";
      return tb.localeCompare(ta);
    });
    return out;
  }, [sessions, row.configLabel, side]);

  return (
    <>
      <tr
        className={
          "compare-allsessions-row" +
          (isExpanded ? " compare-allsessions-row-open" : "")
        }
        onClick={onToggle}
      >
        <td className="compare-allsessions-disc">{isExpanded ? "▾" : "▸"}</td>
        <td>
          <code>{row.configLabel}</code>
        </td>
        <td>{fmtInt(row.nBets)}</td>
        <td>{fmtInt(row.nSettled)}</td>
        <td>
          {row.nWon}-{row.nLost}
        </td>
        <td>{fmtPct(winRate)}</td>
        <td>{fmtMoney(row.totalProfit, { signed: true })}</td>
        <td>{fmtPct(roi)}</td>
        <td>{fmtMoney(profitPerBet, { signed: true })}</td>
        <td>{fmtPct(row.meanAsk)}</td>
        <td>{fmtPct(row.meanFv)}</td>
        <td>{fmtPct(row.meanEdge)}</td>
        <td>{fmtMoney(row.maxDrawdown, { signed: true })}</td>
        <td>{fmtInt(row.daysActive)}</td>
        <td className="compare-allsessions-window">
          {row.firstDate && row.lastDate
            ? row.firstDate === row.lastDate
              ? row.firstDate
              : `${row.firstDate} → ${row.lastDate}`
            : "—"}
        </td>
      </tr>
      {isExpanded && (
        <tr className="compare-allsessions-drill">
          <td colSpan={15}>
            {bets.length === 0 ? (
              <p className="compare-empty">No bets for this model + side.</p>
            ) : (
              <BetsDrilldown bets={bets} />
            )}
          </td>
        </tr>
      )}
    </>
  );
};

const BetsDrilldown: FC<{ bets: Array<BetRow & { _date?: string }> }> = ({
  bets,
}) => {
  return (
    <div className="compare-table-scroll">
      <table className="compare-table compare-allsessions-bets-table">
        <thead>
          <tr>
            <th>Date</th>
            <th>Game</th>
            <th>Side</th>
            <th>Line</th>
            <th>Inn</th>
            <th>Ask</th>
            <th>FV</th>
            <th>Edge</th>
            <th>Stake</th>
            <th>Final</th>
            <th>Result</th>
            <th>P&amp;L</th>
          </tr>
        </thead>
        <tbody>
          {bets.map((b, i) => (
            <tr key={b.bet_id ?? i}>
              <td>{b._date ?? "—"}</td>
              <td>
                {b.away_abbrev && b.home_abbrev
                  ? `${b.away_abbrev}@${b.home_abbrev}`
                  : b.game ?? "—"}
              </td>
              <td>{b.side ?? "—"}</td>
              <td>{b.line ?? "—"}</td>
              <td>
                {b.inning ?? "—"}
                {b.inning_state ? b.inning_state[0] : ""}
              </td>
              <td>{fmtPct(b.entry_ask)}</td>
              <td>{fmtPct(b.fair_value)}</td>
              <td>{fmtPct(b.edge)}</td>
              <td>{fmtMoney(b.stake)}</td>
              <td>{b.final_total != null ? b.final_total : "—"}</td>
              <td
                className={
                  b.won === true
                    ? "compare-allsessions-win"
                    : b.won === false
                      ? "compare-allsessions-loss"
                      : ""
                }
              >
                {b.won === true ? "W" : b.won === false ? "L" : "—"}
              </td>
              <td>{fmtMoney(b.profit, { signed: true })}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
};
