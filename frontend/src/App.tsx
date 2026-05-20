import { useCallback, useEffect, useMemo, useState } from "react";
import type { BetRow, DailyReview, SessionFile } from "./types";
import {
  fetchReview,
  fetchReviewIndex,
  fetchSession,
  fetchSessionIndex,
} from "./api";
import { DateSidebar, type SidebarDateEntry } from "./components/DateSidebar";
import { SessionSummary } from "./components/SessionSummary";
import { NotesPanel } from "./components/NotesPanel";
import { ProgressMilestones } from "./components/ProgressMilestones";
import { WeeklyTable } from "./components/WeeklyTable";
import { BetsTable } from "./components/BetsTable";
import { HealthStatusGrid } from "./components/HealthStatusGrid";

/**
 * Top-level page. Pulls from two backing artifacts:
 *
 *   - daily_human_review/<date>_human_review.json (rich per-day
 *     analysis: 23 health blocks, notes, calibrator state, etc.)
 *   - data/{live,paper}_trading/sessions/<date>_session.json (raw
 *     session data: mode, bets, summary, params)
 *
 * The sidebar shows a UNION of dates from both sources so the
 * operator can navigate to every date the engine ran -- including
 * paper sessions whose daily-review wasn't built (the common case
 * during a paper-mode runway when the operator hasn't run the
 * refresh with `--sessions-dir data/paper_trading/sessions`).
 *
 * When the selected date has a daily_human_review, the full panel
 * stack renders (notes + health blocks + bets). When the date has
 * only a session, the panel stack falls back to a session-only
 * view (summary + bets; no notes/health since those don't exist).
 */
const TRAILING_DAYS_FOR_HISTORY = 30;

export default function App() {
  const [reviewDates, setReviewDates] = useState<string[]>([]);
  const [sessions, setSessions] = useState<SessionFile[]>([]);
  const [selectedDate, setSelectedDate] = useState<string | null>(null);
  const [review, setReview] = useState<DailyReview | null>(null);
  const [trailingReviews, setTrailingReviews] = useState<DailyReview[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  // Initial: fetch review index. Used for the trailing window +
  // for figuring out which sidebar dates have full daily-reviews.
  useEffect(() => {
    fetchReviewIndex()
      .then(({ dates }) => setReviewDates(dates))
      .catch((e) => setError(String(e)));
  }, []);

  // Trailing window of daily reviews (for ProgressMilestones).
  useEffect(() => {
    if (reviewDates.length === 0) return;
    const trailing = reviewDates.slice(-TRAILING_DAYS_FOR_HISTORY).reverse();
    Promise.allSettled(trailing.map((d) => fetchReview(d))).then((results) => {
      const ok: DailyReview[] = [];
      for (const r of results) {
        if (r.status === "fulfilled") ok.push(r.value);
      }
      setTrailingReviews(ok);
    });
  }, [reviewDates]);

  // All sessions (both modes). Feeds the sidebar + WeeklyTable.
  useEffect(() => {
    fetchSessionIndex()
      .then(async ({ sessions: entries }) => {
        const results = await Promise.allSettled(
          entries.map((e) => fetchSession(e.modeFolder, e.date)),
        );
        const ok: SessionFile[] = [];
        for (const r of results) {
          if (r.status === "fulfilled") ok.push(r.value);
        }
        setSessions(ok);
      })
      .catch(() => {
        // Sessions are an enhancement; surface no error.
      });
  }, []);

  // Sidebar entries: union of review dates + session dates, with a
  // mode + has-review indicator per row.
  const sidebarEntries = useMemo<SidebarDateEntry[]>(() => {
    const byDate = new Map<string, SidebarDateEntry>();
    for (const d of reviewDates) {
      byDate.set(d, { date: d, hasReview: true, modes: [] });
    }
    for (const s of sessions) {
      if (!s.date) continue;
      const existing = byDate.get(s.date);
      const mode = s.mode ?? "unknown";
      if (existing) {
        if (!existing.modes.includes(mode)) existing.modes.push(mode);
      } else {
        byDate.set(s.date, {
          date: s.date,
          hasReview: false,
          modes: [mode],
        });
      }
    }
    // Sort modes within each entry: live -> paper -> rest.
    for (const e of byDate.values()) {
      e.modes.sort((a, b) => {
        const rank = (m: string) =>
          m === "live" ? 0 : m === "paper" ? 1 : 2;
        const r = rank(a) - rank(b);
        return r !== 0 ? r : a.localeCompare(b);
      });
    }
    return Array.from(byDate.values()).sort((a, b) =>
      a.date.localeCompare(b.date),
    );
  }, [reviewDates, sessions]);

  // Auto-select the newest available date as soon as we know about
  // any (either from reviews or sessions).
  useEffect(() => {
    if (selectedDate !== null) return;
    if (sidebarEntries.length === 0) return;
    const newest = sidebarEntries[sidebarEntries.length - 1].date;
    setSelectedDate(newest);
  }, [sidebarEntries, selectedDate]);

  // On selected-date change: try the daily-review first; if that
  // 404s, leave review=null and the render path falls back to a
  // session-only synthesized view.
  useEffect(() => {
    if (!selectedDate) return;
    setLoading(true);
    setError(null);
    setReview(null);
    fetchReview(selectedDate)
      .then((r) => setReview(r))
      .catch(() => {
        // No daily-review for this date. Not an error -- the
        // session-only view will render. Don't surface to the
        // user.
      })
      .finally(() => setLoading(false));
  }, [selectedDate]);

  const onSelect = useCallback((date: string) => {
    setSelectedDate(date);
  }, []);

  const lastN = useMemo(
    () => trailingReviews.slice(0, TRAILING_DAYS_FOR_HISTORY),
    [trailingReviews],
  );

  // Per-day view source. Prefer the rich daily-review when it
  // exists; otherwise synthesize a review-shaped object from
  // whichever session(s) match the selected date. When multiple
  // sessions match (live + paper on the same day), prefer live
  // for the per-day panel so it matches the operationally
  // important view -- the operator can still see the paper data
  // in the WeeklyTable's per-mode subtotals.
  const sessionsForSelectedDate = useMemo(
    () => sessions.filter((s) => s.date === selectedDate),
    [sessions, selectedDate],
  );
  const sessionOnlyReview = useMemo<DailyReview | null>(() => {
    if (sessionsForSelectedDate.length === 0) return null;
    // Prefer live > paper > rest when multiple sessions exist.
    const chosen = [...sessionsForSelectedDate].sort((a, b) => {
      const rank = (m: string | undefined) =>
        m === "live" ? 0 : m === "paper" ? 1 : 2;
      return rank(a.mode) - rank(b.mode);
    })[0];
    return sessionToReviewShape(chosen);
  }, [sessionsForSelectedDate]);

  const reviewToDisplay: DailyReview | null = review ?? sessionOnlyReview;
  const isSessionOnly = !review && !!sessionOnlyReview;

  return (
    <div className="app">
      <DateSidebar
        entries={sidebarEntries}
        selectedDate={selectedDate}
        onSelect={onSelect}
      />
      <main className="main">
        <header className="app-header">
          <div>
            <strong>MLB Polymarket Bot</strong> — Daily Review
          </div>
          <div className="app-subtitle">
            {sidebarEntries.length > 0
              ? `${sidebarEntries.length} date${sidebarEntries.length === 1 ? "" : "s"} (${reviewDates.length} with full review, ${sidebarEntries.length - reviewDates.length} session-only)`
              : "loading…"}
          </div>
        </header>
        {error && <div className="error-banner">{error}</div>}
        {loading && <div className="loading-banner">Loading {selectedDate}…</div>}
        {!error && lastN.length > 0 && (
          <ProgressMilestones reviews={lastN} />
        )}
        {!error && sessions.length > 0 && (
          <WeeklyTable sessions={sessions} />
        )}
        {!error && reviewToDisplay && (
          <>
            {isSessionOnly && (
              <div className="info-banner">
                Session-only view for <strong>{selectedDate}</strong> — no
                daily-review was built for this date. Showing summary +
                bets from the raw session JSON; notes &amp; health blocks
                are unavailable until the daily refresh runs against this
                mode.
              </div>
            )}
            <SessionSummary review={reviewToDisplay} />
            {!isSessionOnly && <NotesPanel review={reviewToDisplay} />}
            <BetsTable review={reviewToDisplay} />
            {!isSessionOnly && <HealthStatusGrid review={reviewToDisplay} />}
          </>
        )}
      </main>
    </div>
  );
}

/**
 * Build a DailyReview-shaped object from a session JSON. The
 * components downstream (SessionSummary, BetsTable) already read
 * from both `session_summary` and `bet_totals`; we populate both
 * from the session's `summary` block so the rendering logic
 * doesn't need to know whether it's seeing a real review or a
 * session-only synthesis.
 */
function sessionToReviewShape(session: SessionFile): DailyReview {
  const bets: BetRow[] = session.bets ?? [];
  const wins = bets.filter((b) => b.won === true).length;
  const losses = bets.filter((b) => b.won === false).length;
  const decided = wins + losses;
  const summary = session.summary ?? {};
  return {
    schema_version: 1,
    session_date: session.date,
    mode: session.mode,
    session_summary: {
      orders_placed: bets.length,
      orders_filled: bets.length,
      wins,
      losses,
      total_profit: typeof summary.total_profit === "number" ? summary.total_profit : null,
      total_bets: typeof summary.total_bets === "number" ? summary.total_bets : null,
      settled: typeof summary.settled === "number" ? summary.settled : null,
      roi:
        typeof summary.total_profit === "number" &&
        typeof summary.total_staked === "number" &&
        summary.total_staked > 0
          ? summary.total_profit / summary.total_staked
          : null,
    },
    bet_totals: {
      count: bets.length,
      filled: bets.length,
      wins,
      losses,
      profit: typeof summary.total_profit === "number" ? summary.total_profit : 0,
      win_rate: decided > 0 ? wins / decided : null,
      roi:
        typeof summary.total_profit === "number" &&
        typeof summary.total_staked === "number" &&
        summary.total_staked > 0
          ? summary.total_profit / summary.total_staked
          : null,
    },
    bets,
    notes: [],
  };
}
