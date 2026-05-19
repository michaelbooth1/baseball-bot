import type { DailyReview, ReviewIndex } from "./types";

/** Fetch the list of available review dates from the dev middleware. */
export async function fetchReviewIndex(): Promise<ReviewIndex> {
  const r = await fetch("/api/reviews");
  if (!r.ok) {
    throw new Error(`fetchReviewIndex failed: ${r.status} ${r.statusText}`);
  }
  return (await r.json()) as ReviewIndex;
}

/** Fetch a single date's review JSON. */
export async function fetchReview(date: string): Promise<DailyReview> {
  const r = await fetch(`/api/reviews/${date}`);
  if (!r.ok) {
    throw new Error(`fetchReview(${date}) failed: ${r.status} ${r.statusText}`);
  }
  return (await r.json()) as DailyReview;
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
