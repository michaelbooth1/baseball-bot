import { useCallback, useEffect, useMemo, useState } from "react";
import type {
  BetRow,
  DailyReview,
  SessionFile,
  SessionIndexEntry,
} from "./types";
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
import { CompareView } from "./components/CompareView";
import { MultiEngineDayView } from "./components/MultiEngineDayView";

/** Top-level view selector. "sessions" = legacy per-day panel;
 *  "compare" = new multi-engine comparison page (2026-05-25). */
type TopLevelView = "sessions" | "compare";

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
  // 2026-05-25: keep the index entries (modeFolder + configLabel)
  // alongside the parsed session JSONs so the sidebar can render
  // per-config sub-rows for multi-engine runs.
  const [sessionIndex, setSessionIndex] = useState<SessionIndexEntry[]>([]);
  const [selectedDate, setSelectedDate] = useState<string | null>(null);
  // When set, the per-day view renders THIS specific session instead
  // of the date-level default (live > paper > rest). Cleared when
  // the operator clicks a bare date row.
  const [selectedSession, setSelectedSession] = useState<
    { date: string; modeFolder: string } | null
  >(null);
  const [topView, setTopView] = useState<TopLevelView>("sessions");
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

  // All sessions (every discovered modeFolder). Feeds sidebar + WeeklyTable.
  useEffect(() => {
    fetchSessionIndex()
      .then(async ({ sessions: entries }) => {
        setSessionIndex(entries);
        const results = await Promise.allSettled(
          entries.map((e) => fetchSession(e.modeFolder, e.date)),
        );
        const ok: SessionFile[] = [];
        for (let i = 0; i < results.length; i++) {
          const r = results[i];
          if (r.status === "fulfilled") {
            // 2026-05-26: stamp the (modeFolder, configLabel) on the
            // loaded SessionFile so MultiEngineDayView can map a
            // session back to its config without an extra lookup. The
            // underlying JSON on disk does NOT carry these fields --
            // they live on the index entry.
            const enriched: SessionFile = {
              ...r.value,
              _modeFolder: entries[i].modeFolder,
              _configLabel: entries[i].configLabel,
            };
            ok.push(enriched);
          }
        }
        setSessions(ok);
      })
      .catch(() => {
        // Sessions are an enhancement; surface no error.
      });
  }, []);

  // Sidebar entries: union of review dates + session index entries.
  // Uses the INDEX (not the loaded session files) so we get full
  // (modeFolder, mode, configLabel) tuples for per-config sub-row
  // rendering, even before all session files have finished loading.
  const sidebarEntries = useMemo<SidebarDateEntry[]>(() => {
    const byDate = new Map<string, SidebarDateEntry>();
    for (const d of reviewDates) {
      byDate.set(d, { date: d, hasReview: true, modes: [], sessions: [] });
    }
    for (const e of sessionIndex) {
      const existing = byDate.get(e.date);
      const mode = e.mode || "unknown";
      const sessionEntry = {
        modeFolder: e.modeFolder,
        mode,
        configLabel: e.configLabel,
      };
      if (existing) {
        if (!existing.modes.includes(mode)) existing.modes.push(mode);
        existing.sessions.push(sessionEntry);
      } else {
        byDate.set(e.date, {
          date: e.date,
          hasReview: false,
          modes: [mode],
          sessions: [sessionEntry],
        });
      }
    }
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
  }, [reviewDates, sessionIndex]);

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
    // Clicking the bare date row clears any per-session pin so the
    // panel reverts to the date-level default (live > paper > rest).
    setSelectedSession(null);
  }, []);

  const onSelectSession = useCallback(
    (date: string, modeFolder: string) => {
      setSelectedDate(date);
      setSelectedSession({ date, modeFolder });
    },
    [],
  );

  // Fetch the explicit per-config session JSON when one is pinned.
  // Falls back silently to the date-level default if the fetch fails.
  const [pinnedSession, setPinnedSession] = useState<SessionFile | null>(null);
  useEffect(() => {
    if (!selectedSession) {
      setPinnedSession(null);
      return;
    }
    fetchSession(selectedSession.modeFolder, selectedSession.date)
      .then((s) => setPinnedSession(s))
      .catch(() => setPinnedSession(null));
  }, [selectedSession]);

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
    // When the operator has pinned a specific per-config session,
    // use that one directly (overrides date-level defaulting).
    if (pinnedSession) return sessionToReviewShape(pinnedSession);
    if (sessionsForSelectedDate.length === 0) return null;
    // Default: prefer live > paper > rest.
    const chosen = [...sessionsForSelectedDate].sort((a, b) => {
      const rank = (m: string | undefined) =>
        m === "live" ? 0 : m === "paper" ? 1 : 2;
      return rank(a.mode) - rank(b.mode);
    })[0];
    return sessionToReviewShape(chosen);
  }, [pinnedSession, sessionsForSelectedDate]);

  // When a specific session is pinned, ALWAYS show that one's data
  // (don't fall back to the full daily-review, which is per-DATE
  // not per-CONFIG and would mix engines).
  const reviewToDisplay: DailyReview | null = selectedSession
    ? sessionOnlyReview
    : (review ?? sessionOnlyReview);
  const isSessionOnly = !!selectedSession || (!review && !!sessionOnlyReview);

  // 2026-05-26: when the selected date is a multi-engine date AND no
  // specific config is pinned in the sidebar, render the all-engines
  // overview instead of a single engine's panel. The operator can
  // still click a sidebar sub-row to pin one config and drop back to
  // the single-engine detail. Multi-engine = ≥2 loaded sessions
  // that carry a configLabel from the index (a single live + paper
  // pair counts as legacy, not multi-engine).
  const multiEngineSessions = useMemo<SessionFile[]>(() => {
    if (!selectedDate) return [];
    const dateSessions = sessions.filter(
      (s) => s.date === selectedDate && !!s._configLabel,
    );
    return dateSessions.length >= 2 ? dateSessions : [];
  }, [selectedDate, sessions]);

  const showMultiEngineDayView =
    !selectedSession && multiEngineSessions.length >= 2;

  return (
    <div className="app">
      <DateSidebar
        entries={sidebarEntries}
        selectedDate={selectedDate}
        selectedSession={selectedSession}
        onSelect={onSelect}
        onSelectSession={onSelectSession}
      />
      <main className="main">
        <header className="app-header">
          <div>
            <strong>MLB Polymarket Bot</strong> — Daily Review
          </div>
          <nav className="app-tabs" aria-label="Top-level view">
            <button
              type="button"
              className={
                "app-tab" + (topView === "sessions" ? " app-tab-active" : "")
              }
              onClick={() => setTopView("sessions")}
            >
              Sessions
            </button>
            <button
              type="button"
              className={
                "app-tab" + (topView === "compare" ? " app-tab-active" : "")
              }
              onClick={() => setTopView("compare")}
            >
              Compare engines
            </button>
          </nav>
          <div className="app-subtitle">
            {sidebarEntries.length > 0
              ? `${sidebarEntries.length} date${sidebarEntries.length === 1 ? "" : "s"} (${reviewDates.length} with full review, ${sidebarEntries.length - reviewDates.length} session-only)`
              : "loading…"}
          </div>
        </header>
        {topView === "compare" ? (
          <CompareView />
        ) : showMultiEngineDayView ? (
          <MultiEngineDayViewBody
            error={error}
            loading={loading}
            selectedDate={selectedDate!}
            sessions={multiEngineSessions}
            lastN={lastN}
            allSessions={sessions}
          />
        ) : (
          <SessionsViewBody
            error={error}
            loading={loading}
            selectedDate={selectedDate}
            lastN={lastN}
            sessions={sessions}
            reviewToDisplay={reviewToDisplay}
            isSessionOnly={isSessionOnly}
          />
        )}
      </main>
    </div>
  );
}

/** Inner body of the "Sessions" tab. Extracted so the tab switch
 *  doesn't bloat the App component. Pure render of existing panels. */
type SessionsViewBodyProps = {
  error: string | null;
  loading: boolean;
  selectedDate: string | null;
  lastN: DailyReview[];
  sessions: SessionFile[];
  reviewToDisplay: DailyReview | null;
  isSessionOnly: boolean;
};

function SessionsViewBody({
  error,
  loading,
  selectedDate,
  lastN,
  sessions,
  reviewToDisplay,
  isSessionOnly,
}: SessionsViewBodyProps) {
  return (
    <>
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
    </>
  );
}

/** Inner body of the "Sessions" tab when the selected date is a
 *  multi-engine date. Keeps the ProgressMilestones + WeeklyTable on
 *  top (so the operator still sees the trailing-30d context) and then
 *  renders the all-engines comparison + per-engine details below.
 *  2026-05-26: added so multi-engine dates show all models at once
 *  instead of forcing the operator to click each per-config sub-row. */
type MultiEngineDayViewBodyProps = {
  error: string | null;
  loading: boolean;
  selectedDate: string;
  sessions: SessionFile[];
  lastN: DailyReview[];
  allSessions: SessionFile[];
};

function MultiEngineDayViewBody({
  error,
  loading,
  selectedDate,
  sessions,
  lastN,
  allSessions,
}: MultiEngineDayViewBodyProps) {
  // Build (modeFolder -> configLabel) and (session -> modeFolder)
  // lookups for MultiEngineDayView. The stamps were applied at load
  // time in App's useEffect so this is just a projection.
  const configLabelByModeFolder = useMemo(() => {
    const out: Record<string, string | undefined> = {};
    for (const s of sessions) {
      if (s._modeFolder) out[s._modeFolder] = s._configLabel;
    }
    return out;
  }, [sessions]);
  const modeFolderBySession = useMemo(() => {
    const out = new Map<SessionFile, string>();
    for (const s of sessions) {
      if (s._modeFolder) out.set(s, s._modeFolder);
    }
    return out;
  }, [sessions]);
  return (
    <>
      {error && <div className="error-banner">{error}</div>}
      {loading && <div className="loading-banner">Loading {selectedDate}…</div>}
      {!error && lastN.length > 0 && <ProgressMilestones reviews={lastN} />}
      {!error && allSessions.length > 0 && <WeeklyTable sessions={allSessions} />}
      <MultiEngineDayView
        date={selectedDate}
        sessions={sessions}
        configLabelByModeFolder={configLabelByModeFolder}
        modeFolderBySession={modeFolderBySession}
      />
    </>
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
