import { useCallback, useEffect, useMemo, useState } from "react";
import type { DailyReview } from "./types";
import { fetchReview, fetchReviewIndex } from "./api";
import { DateSidebar } from "./components/DateSidebar";
import { SessionSummary } from "./components/SessionSummary";
import { NotesPanel } from "./components/NotesPanel";
import { ProgressMilestones } from "./components/ProgressMilestones";
import { WeeklyTable } from "./components/WeeklyTable";
import { BetsTable } from "./components/BetsTable";
import { HealthStatusGrid } from "./components/HealthStatusGrid";

/**
 * Top-level page. Loads the date index on mount, auto-selects the
 * newest date, fetches that date's review JSON, and renders the
 * stacked panels. The progress-milestones + weekly-table panels
 * need the last N reviews aggregated, so we also lazily fetch the
 * trailing-N dates to power them. 30 days = ~4-5 ISO weeks, which
 * is the sweet spot for weekly roll-up granularity at the bot's
 * typical pace.
 */
const TRAILING_DAYS_FOR_HISTORY = 30;

export default function App() {
  const [dates, setDates] = useState<string[]>([]);
  const [selectedDate, setSelectedDate] = useState<string | null>(null);
  const [review, setReview] = useState<DailyReview | null>(null);
  const [trailingReviews, setTrailingReviews] = useState<DailyReview[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  // Initial: fetch index + select latest
  useEffect(() => {
    fetchReviewIndex()
      .then(({ dates }) => {
        setDates(dates);
        if (dates.length > 0) setSelectedDate(dates[dates.length - 1]);
        else setError("No reviews available. Run the daily refresh first.");
      })
      .catch((e) => setError(String(e)));
  }, []);

  // On date change: fetch the selected review
  useEffect(() => {
    if (!selectedDate) return;
    setLoading(true);
    setError(null);
    fetchReview(selectedDate)
      .then((r) => setReview(r))
      .catch((e) => setError(String(e)))
      .finally(() => setLoading(false));
  }, [selectedDate]);

  // Trailing window — refetch when dates change. Used by both
  // ProgressMilestones (treats as "trailing 30d aggregate") and
  // WeeklyTable (groups into ISO weeks).
  useEffect(() => {
    if (dates.length === 0) return;
    const trailing = dates.slice(-TRAILING_DAYS_FOR_HISTORY).reverse();
    Promise.allSettled(trailing.map((d) => fetchReview(d))).then((results) => {
      const ok: DailyReview[] = [];
      for (const r of results) {
        if (r.status === "fulfilled") ok.push(r.value);
      }
      setTrailingReviews(ok);
    });
  }, [dates]);

  const onSelect = useCallback((date: string) => {
    setSelectedDate(date);
  }, []);

  const lastN = useMemo(
    () => trailingReviews.slice(0, TRAILING_DAYS_FOR_HISTORY),
    [trailingReviews],
  );

  return (
    <div className="app">
      <DateSidebar
        dates={dates}
        selectedDate={selectedDate}
        onSelect={onSelect}
      />
      <main className="main">
        <header className="app-header">
          <div>
            <strong>MLB Polymarket Bot</strong> — Daily Review
          </div>
          <div className="app-subtitle">
            {dates.length > 0
              ? `${dates.length} review${dates.length === 1 ? "" : "s"} on disk`
              : "loading…"}
          </div>
        </header>
        {error && <div className="error-banner">{error}</div>}
        {loading && <div className="loading-banner">Loading {selectedDate}…</div>}
        {!error && lastN.length > 0 && (
          <>
            <ProgressMilestones reviews={lastN} />
            <WeeklyTable reviews={lastN} />
          </>
        )}
        {!error && review && (
          <>
            <SessionSummary review={review} />
            <NotesPanel review={review} />
            <BetsTable review={review} />
            <HealthStatusGrid review={review} />
          </>
        )}
      </main>
    </div>
  );
}
