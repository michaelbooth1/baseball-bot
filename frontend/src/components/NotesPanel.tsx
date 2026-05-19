import type { FC } from "react";
import type { DailyReview } from "../types";

type Props = {
  review: DailyReview;
};

/**
 * The top-level `notes` block of the daily review carries the
 * mirrored alerts from every health block (prefixed with the block
 * name: `Cohort-roi:`, `Stage1-shadow:`, `Under-outcomes:`, etc.).
 *
 * We color each note by prefix-category so the operator can scan
 * for severity at a glance: drift-class alerts (calibration, cohort,
 * concept) get warning color; informational lines (filled ROI line)
 * are neutral.
 */
export const NotesPanel: FC<Props> = ({ review }) => {
  const notes = review.notes ?? [];
  if (notes.length === 0) {
    return (
      <section className="card">
        <h2 className="card-title">Notes</h2>
        <p className="empty-state">No alerts or notes for this session.</p>
      </section>
    );
  }
  return (
    <section className="card">
      <h2 className="card-title">Notes ({notes.length})</h2>
      <ul className="notes-list">
        {notes.map((n, i) => (
          <li key={i} className={"note note-" + classifyNote(n)}>
            {n}
          </li>
        ))}
      </ul>
    </section>
  );
};

/**
 * Note severity classifier — purely visual. Drift-class alerts and
 * regressions are `warn`; promote/scoped-promotion suggestions are
 * `info`; everything else is `neutral`.
 */
function classifyNote(note: string): "warn" | "info" | "neutral" {
  const lower = note.toLowerCase();
  if (
    lower.includes("regress") ||
    lower.includes("drift") ||
    lower.includes("stale") ||
    lower.includes("loss-making") ||
    lower.includes("pending") ||
    lower.includes("disagree")
  ) {
    return "warn";
  }
  if (
    lower.includes("promote") ||
    lower.includes("would have netted") ||
    lower.includes("consider")
  ) {
    return "info";
  }
  return "neutral";
}
