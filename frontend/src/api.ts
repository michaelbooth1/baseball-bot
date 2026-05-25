import type {
  DailyReview,
  ParallelComparison,
  ParallelComparisonIndex,
  ReviewIndex,
  SessionFile,
  SessionIndex,
} from "./types";

/** Fetch the list of available review dates from the dev middleware. */
export async function fetchReviewIndex(): Promise<ReviewIndex> {
  const r = await fetch("/api/reviews");
  if (!r.ok) {
    throw new Error(`fetchReviewIndex failed: ${r.status} ${r.statusText}`);
  }
  return (await r.json()) as ReviewIndex;
}

/** Fetch a single date's daily-review JSON. */
export async function fetchReview(date: string): Promise<DailyReview> {
  const r = await fetch(`/api/reviews/${date}`);
  if (!r.ok) {
    throw new Error(`fetchReview(${date}) failed: ${r.status} ${r.statusText}`);
  }
  return (await r.json()) as DailyReview;
}

/**
 * Fetch the full list of session files (both live + paper). Each entry
 * carries (date, mode, modeFolder) so the consumer can later fetch the
 * full session JSON via `fetchSession(modeFolder, date)`.
 *
 * Session files exist for EVERY date the engine ran, regardless of
 * whether a daily_human_review was built for that date. So
 * aggregating by week from sessions covers all dates while daily-
 * reviews would miss dates whose `--sessions-dir` wasn't passed at
 * refresh time.
 */
export async function fetchSessionIndex(): Promise<SessionIndex> {
  const r = await fetch("/api/sessions");
  if (!r.ok) {
    throw new Error(`fetchSessionIndex failed: ${r.status} ${r.statusText}`);
  }
  return (await r.json()) as SessionIndex;
}

/** Fetch a single session JSON by (modeFolder, date). */
export async function fetchSession(
  modeFolder: string,
  date: string,
): Promise<SessionFile> {
  const r = await fetch(`/api/sessions/${modeFolder}/${date}`);
  if (!r.ok) {
    throw new Error(
      `fetchSession(${modeFolder}, ${date}) failed: ${r.status} ${r.statusText}`,
    );
  }
  return (await r.json()) as SessionFile;
}

/** Fetch the list of available parallel-engine comparison ranges
 *  (2026-05-25+). Each entry is a `<start>_<end>` date-range slug
 *  matching a file produced by `aggregate_parallel_engines.py`. */
export async function fetchParallelComparisonIndex(): Promise<ParallelComparisonIndex> {
  const r = await fetch("/api/parallel-comparisons");
  if (!r.ok) {
    throw new Error(
      `fetchParallelComparisonIndex failed: ${r.status} ${r.statusText}`,
    );
  }
  return (await r.json()) as ParallelComparisonIndex;
}

/** Fetch a single parallel-engine comparison report by date range. */
export async function fetchParallelComparison(
  range: string,
): Promise<ParallelComparison> {
  const r = await fetch(`/api/parallel-comparisons/${range}`);
  if (!r.ok) {
    throw new Error(
      `fetchParallelComparison(${range}) failed: ${r.status} ${r.statusText}`,
    );
  }
  return (await r.json()) as ParallelComparison;
}

/** Pretty-print a number as USD with sign. */
export function fmtMoney(v: number | null | undefined, opts: { signed?: boolean } = {}): string {
  if (v === null || v === undefined || Number.isNaN(v)) return "—";
  const sign = opts.signed && v >= 0 ? "+" : "";
  return `${sign}$${v.toFixed(2)}`;
}

/** Pretty-print a 0..1 ratio as a percentage. */
export function fmtPct(v: number | null | undefined, digits = 1): string {
  if (v === null || v === undefined || Number.isNaN(v)) return "—";
  return `${(v * 100).toFixed(digits)}%`;
}

/** Safe integer formatter. */
export function fmtInt(v: number | null | undefined): string {
  if (v === null || v === undefined || Number.isNaN(v)) return "—";
  return Math.round(v).toString();
}
