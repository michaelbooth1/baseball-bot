import type { FC } from "react";

export type SidebarDateEntry = {
  date: string;
  /** True when a daily_human_review JSON exists for this date. */
  hasReview: boolean;
  /** Modes that have a session file for this date (e.g., {"paper"} or {"live", "paper"}). */
  modes: string[];
};

type Props = {
  entries: SidebarDateEntry[];
  selectedDate: string | null;
  onSelect: (date: string) => void;
};

/**
 * Left rail with all available dates (union of daily_human_review
 * + session files), newest first. Each row shows a compact mode
 * indicator so the operator can tell whether the date has a paper
 * session, a live session, or both -- and whether a full
 * daily_human_review was built or only the raw session is
 * available.
 *
 * Indicator legend:
 *   ✓ = full daily_human_review built (rich health blocks + notes)
 *   (no checkmark) = session-only (engine ran but refresh didn't
 *     build a daily-review for this date/mode)
 */
export const DateSidebar: FC<Props> = ({
  entries,
  selectedDate,
  onSelect,
}) => {
  const display = [...entries].sort((a, b) => b.date.localeCompare(a.date));
  return (
    <aside className="sidebar">
      <h2 className="sidebar-title">Sessions</h2>
      <p className="sidebar-meta">{entries.length} dates available</p>
      <ul className="date-list">
        {display.map((e) => (
          <li key={e.date}>
            <button
              type="button"
              className={
                "date-button" +
                (e.date === selectedDate ? " date-button-selected" : "")
              }
              onClick={() => onSelect(e.date)}
            >
              <span className="date-label">{e.date}</span>
              <span className="date-tags">
                {e.modes.map((m) => (
                  <span key={m} className={"date-tag date-tag-" + m}>
                    {m}
                  </span>
                ))}
                {e.hasReview && <span className="date-review-mark">✓</span>}
              </span>
            </button>
          </li>
        ))}
      </ul>
    </aside>
  );
};
