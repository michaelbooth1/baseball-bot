import type { FC } from "react";

type Props = {
  dates: string[];
  selectedDate: string | null;
  onSelect: (date: string) => void;
};

/**
 * Left rail with the available review dates, newest first. The
 * server returns dates lexicographically; we reverse for display.
 */
export const DateSidebar: FC<Props> = ({ dates, selectedDate, onSelect }) => {
  const displayDates = [...dates].reverse();
  return (
    <aside className="sidebar">
      <h2 className="sidebar-title">Reviews</h2>
      <p className="sidebar-meta">{dates.length} dates available</p>
      <ul className="date-list">
        {displayDates.map((d) => (
          <li key={d}>
            <button
              type="button"
              className={
                "date-button" + (d === selectedDate ? " date-button-selected" : "")
              }
              onClick={() => onSelect(d)}
            >
              {d}
            </button>
          </li>
        ))}
      </ul>
    </aside>
  );
};
