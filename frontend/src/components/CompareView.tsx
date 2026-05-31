import { useEffect, useState, type FC } from "react";
import {
  fetchParallelComparison,
  fetchParallelComparisonIndex,
  fmtInt,
  fmtMoney,
  fmtPct,
} from "../api";
import type { ParallelComparison, SessionFile } from "../types";
import { AllSessionsBreakdown } from "./AllSessionsBreakdown";

/**
 * Multi-engine comparison page (2026-05-25). Renders two layers:
 *
 *   1. AllSessionsBreakdown (2026-05-30): cross-date per-model totals
 *      computed client-side from every loaded session, so the operator
 *      sees ALL bets each model has placed in one table. This is the
 *      "main comparison" view the operator opens to read model
 *      performance at a glance, no backend aggregation required.
 *
 *   2. The original parallel-comparison range tables (headline /
 *      normalized / daily-read / shared-candidate disagreement),
 *      sourced from
 *      `data/analysis_output/parallel_engine_comparison/parallel_engine_comparison_<range>.json`.
 *      These remain for windowed analysis (e.g., last-7d cohort
 *      effects, shared-candidate disagreement which needs the
 *      offline aggregator's funnel data).
 */
type CompareViewProps = {
  /** All loaded session JSONs, stamped with `_configLabel` and
   *  `_modeFolder` by App.tsx at load time. Used by the all-sessions
   *  breakdown table at the top. */
  sessions: SessionFile[];
};

export const CompareView: FC<CompareViewProps> = ({ sessions }) => {
  const [ranges, setRanges] = useState<string[]>([]);
  const [selectedRange, setSelectedRange] = useState<string | null>(null);
  const [report, setReport] = useState<ParallelComparison | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Load the index of available comparison ranges; auto-select newest.
  useEffect(() => {
    fetchParallelComparisonIndex()
      .then((idx) => {
        setRanges(idx.ranges);
        if (idx.ranges.length > 0 && selectedRange === null) {
          setSelectedRange(idx.ranges[idx.ranges.length - 1]);
        }
      })
      .catch((e) => setError(String(e)));
  }, [selectedRange]);

  // Load the report whenever the selection changes.
  useEffect(() => {
    if (!selectedRange) return;
    setLoading(true);
    setError(null);
    setReport(null);
    fetchParallelComparison(selectedRange)
      .then((r) => setReport(r))
      .catch((e) => setError(String(e)))
      .finally(() => setLoading(false));
  }, [selectedRange]);

  return (
    <div className="compare-view">
      <header className="compare-header">
        <h2>Parallel Engine Comparison</h2>
        <div className="compare-range-picker">
          <label htmlFor="compare-range-select">Range report:</label>
          <select
            id="compare-range-select"
            value={selectedRange ?? ""}
            onChange={(e) => setSelectedRange(e.target.value || null)}
          >
            {ranges.length === 0 && <option value="">(no reports yet)</option>}
            {ranges.map((r) => (
              <option key={r} value={r}>
                {r}
              </option>
            ))}
          </select>
        </div>
      </header>

      <AllSessionsBreakdown sessions={sessions} />

      <h3 className="compare-section-divider">
        Range report ({selectedRange ?? "—"})
      </h3>

      {loading && <p>Loading…</p>}
      {error && (
        <p className="compare-error">
          Failed to load comparison: <code>{error}</code>
        </p>
      )}

      {report && (
        <>
          <DailyReadBlock report={report} />
          <NormalizedTable report={report} />
          <HeadlineTable report={report} />
          <DisagreementTable report={report} />
        </>
      )}
    </div>
  );
};

const DailyReadBlock: FC<{ report: ParallelComparison }> = ({ report }) => {
  const read = report.daily_read;
  if (!read) return null;
  return (
    <section className="compare-section">
      <h3>Daily read</h3>
      <ul className="compare-daily-read">
        {read.baseline_config_label && (
          <li>
            Volume-index baseline: <code>{read.baseline_config_label}</code>
          </li>
        )}
        <li>
          Best ROI so far: <strong>{read.best_roi_config ?? "—"}</strong>{" "}
          ({fmtPct(read.best_roi)})
        </li>
        {read.best_profit_per_settled_bet_config && (
          <li>
            Best <strong>$/Bet</strong> (per-bet quality, independent of volume):{" "}
            <strong>{read.best_profit_per_settled_bet_config}</strong>{" "}
            ({fmtMoney(read.best_profit_per_settled_bet, { signed: true })})
          </li>
        )}
        <li>
          Lowest drawdown so far:{" "}
          <strong>{read.lowest_drawdown_config ?? "—"}</strong>{" "}
          ({fmtMoney(read.lowest_drawdown, { signed: true })})
        </li>
        <li>
          Split opportunities: game-line={read.game_line_splits ?? 0}, fine-state=
          {read.fine_state_splits ?? 0}
        </li>
        {read.sample_flags && read.sample_flags.length > 0 && (
          <li className="compare-sample-warning">
            <strong>Sample warning:</strong> {read.sample_flags.join("; ")}{" "}
            Treat rankings as diagnostic, not promotion evidence.
          </li>
        )}
      </ul>
    </section>
  );
};

const HeadlineTable: FC<{ report: ParallelComparison }> = ({ report }) => {
  const configs = report.configs;
  if (!configs) return null;
  const labels = Object.keys(configs).sort();
  return (
    <section className="compare-section">
      <h3>Per-config headline</h3>
      <div className="compare-table-scroll">
        <table className="compare-table">
          <thead>
            <tr>
              <th>Config</th>
              <th>Bets</th>
              <th>Settled</th>
              <th>W-L</th>
              <th>WR</th>
              <th>Stake</th>
              <th>P&amp;L</th>
              <th>ROI</th>
              <th>Max DD</th>
              <th>Mean FV</th>
              <th>Mean Ask</th>
              <th>Stake-wtd Actual−Ask</th>
            </tr>
          </thead>
          <tbody>
            {labels.map((label) => {
              const h = configs[label]?.headline ?? {};
              const won = h.n_won ?? 0;
              const settled = h.n_settled ?? 0;
              const lost = settled - won;
              return (
                <tr key={label}>
                  <td>
                    <code>{label}</code>
                  </td>
                  <td>{fmtInt(h.n_bets)}</td>
                  <td>{fmtInt(h.n_settled)}</td>
                  <td>
                    {won}-{lost}
                  </td>
                  <td>{fmtPct(h.win_rate)}</td>
                  <td>{fmtMoney(h.total_staked)}</td>
                  <td>{fmtMoney(h.total_profit, { signed: true })}</td>
                  <td>{fmtPct(h.roi)}</td>
                  <td>{fmtMoney(h.max_drawdown, { signed: true })}</td>
                  <td>{fmtPct(h.mean_fair_value)}</td>
                  <td>{fmtPct(h.mean_entry_ask)}</td>
                  <td>
                    {fmtPct(h.edge_over_market_stake_weighted_actual_minus_ask)}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </section>
  );
};

/** 2026-05-26: per-bet + cohort breadth view. Sits between the daily
 *  read and the full headline so the operator reads quality before
 *  volume-dependent totals. F_no_dedup's volume-index column makes it
 *  obvious when one config dominates raw P&L purely by trading more. */
const NormalizedTable: FC<{ report: ParallelComparison }> = ({ report }) => {
  const configs = report.configs;
  if (!configs) return null;
  const labels = Object.keys(configs).sort();
  const baseline = report.baseline_config_label;
  return (
    <section className="compare-section">
      <h3>
        Per-config normalized (per-bet + cohort breadth)
        {baseline && (
          <span className="compare-baseline-chip">
            baseline = <code>{baseline}</code>
          </span>
        )}
      </h3>
      <p className="compare-disagreement-summary">
        <strong>$/Bet</strong> is per-bet quality independent of volume;{" "}
        <strong>Volume Idx</strong> &gt; 1 means the config placed more bets
        than the baseline. Use them together to separate quality from volume —
        a config with high $/Bet but Volume Idx ≪ 1 makes great picks rarely,
        while a config with low $/Bet but Volume Idx ≫ 1 is grinding volume.
      </p>
      <div className="compare-table-scroll">
        <table className="compare-table">
          <thead>
            <tr>
              <th>Config</th>
              <th>Bets</th>
              <th>Settled</th>
              <th>Unique GLs</th>
              <th>Bets/GL</th>
              <th>$/Bet</th>
              <th>Volume Idx</th>
              <th>Settled Idx</th>
            </tr>
          </thead>
          <tbody>
            {labels.map((label) => {
              const h = configs[label]?.headline ?? {};
              const bpgl = h.bets_per_unique_game_line;
              const vol = h.volume_index_vs_baseline;
              const settledIdx = h.settled_index_vs_baseline;
              return (
                <tr key={label}>
                  <td>
                    <code>{label}</code>
                  </td>
                  <td>{fmtInt(h.n_bets)}</td>
                  <td>{fmtInt(h.n_settled)}</td>
                  <td>{fmtInt(h.n_unique_game_lines)}</td>
                  <td>
                    {bpgl != null && !Number.isNaN(bpgl)
                      ? bpgl.toFixed(2)
                      : "—"}
                  </td>
                  <td>
                    {fmtMoney(h.profit_per_settled_bet, { signed: true })}
                  </td>
                  <td>
                    {vol != null && !Number.isNaN(vol)
                      ? `${vol.toFixed(2)}x`
                      : "—"}
                  </td>
                  <td>
                    {settledIdx != null && !Number.isNaN(settledIdx)
                      ? `${settledIdx.toFixed(2)}x`
                      : "—"}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </section>
  );
};

const DisagreementTable: FC<{ report: ParallelComparison }> = ({ report }) => {
  const sd = report.shared_candidate_disagreement;
  const gameLine = sd?.game_line;
  if (!gameLine) return null;
  const counts = gameLine.counts ?? {};
  const splits = gameLine.splits ?? [];
  return (
    <section className="compare-section">
      <h3>Shared-candidate disagreement (game-line level)</h3>
      <p className="compare-disagreement-summary">
        Compared <strong>{counts.keys_compared ?? 0}</strong> game-lines:{" "}
        <strong>{counts.unanimous_skip ?? 0}</strong> unanimous skip,{" "}
        <strong>{counts.unanimous_trade ?? 0}</strong> unanimous trade,{" "}
        <strong>{counts.split ?? 0}</strong> split,{" "}
        <strong>{counts.partial_coverage ?? 0}</strong> partial coverage.
      </p>
      {splits.length === 0 ? (
        <p className="compare-empty">No game-line splits in this window.</p>
      ) : (
        <div className="compare-table-scroll">
          <table className="compare-table">
            <thead>
              <tr>
                <th>Key</th>
                <th>Decisions</th>
                <th>Final total</th>
              </tr>
            </thead>
            <tbody>
              {splits.map((s) => (
                <tr key={s.key}>
                  <td>
                    <code>{s.key}</code>
                  </td>
                  <td>
                    {s.decisions
                      ? Object.entries(s.decisions)
                          .map(([k, v]) => `${k}: ${v}`)
                          .join(", ")
                      : "—"}
                  </td>
                  <td>
                    {s.outcome?.final_total ?? "—"}
                    {s.outcome?.final_away !== undefined &&
                    s.outcome?.final_home !== undefined
                      ? ` (${s.outcome.final_away}-${s.outcome.final_home})`
                      : ""}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
};
