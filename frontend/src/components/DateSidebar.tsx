import type { FC } from "react";

/** One session that ran on a given date. The sidebar uses these to
 *  render per-config sub-rows under each date when multi-engine
 *  parallel runs are present. Legacy single-engine sessions still
 *  produce a SidebarSession with `configLabel` undefined. */
export type SidebarSession = {
  modeFolder: string;
  mode: string;
  configLabel?: string;
};

export type SidebarDateEntry = {
  date: string;
  /** True when a daily_human_review JSON exists for this date. */
  hasReview: boolean;
  /** Distinct authoritative-mode labels for this date. Retained for
   *  back-compat with the indicator-chip rendering; the more
   *  granular per-session list lives in `sessions`. */
  modes: string[];
  /** Every session file that touched this date, with its modeFolder
   *  + mode + (for multi-engine sessions) the config label the
   *  launcher stamped on the session params. */
  sessions: SidebarSession[];
};

type Props = {
  entries: SidebarDateEntry[];
  selectedDate: string | null;
  /** Currently-pinned per-config session. When set, App.tsx renders
   *  that specific session's data instead of the date-level default
   *  (live > paper > rest). Null = use the date-level default. */
  selectedSession: { date: string; modeFolder: string } | null;
  onSelect: (date: string) => void;
  /** Click handler for a per-config sub-row (e.g., "A_current"
   *  under 2026-05-25). Sets selectedSession and selectedDate
   *  together. */
  onSelectSession: (date: string, modeFolder: string) => void;
};

/**
 * Left rail with all available dates (union of daily_human_review
 * + session files), newest first. Each date row shows:
 *   - the date label + mode chips + ✓ review-mark (legacy)
 *   - per-config sub-rows when multi-engine parallel sessions exist
 *     (each clickable, pinning the App's per-day view to that
 *     specific config's session)
 *
 * Indicator legend:
 *   ✓ = full daily_human_review built (rich health blocks + notes)
 *   (no checkmark) = session-only (engine ran but refresh didn't
 *     build a daily-review for this date/mode)
 */
export const DateSidebar: FC<Props> = ({
  entries,
  selectedDate,
  selectedSession,
  onSelect,
  onSelectSession,
}) => {
  const display = [...entries].sort((a, b) => b.date.localeCompare(a.date));
  return (
    <aside className="sidebar">
      <h2 className="sidebar-title">Sessions</h2>
      <p className="sidebar-meta">{entries.length} dates available</p>
      <ul className="date-list">
        {display.map((e) => {
          const labelledSessions = e.sessions.filter(
            (s) =>
              s.configLabel ||
              (s.modeFolder !== "live" && s.modeFolder !== "paper"),
          );
          const legacySessions = e.sessions.filter(
            (s) =>
              !s.configLabel &&
              (s.modeFolder === "live" || s.modeFolder === "paper"),
          );
          const sortedLabelled = [...labelledSessions].sort((a, b) =>
            (a.configLabel || a.modeFolder).localeCompare(
              b.configLabel || b.modeFolder,
            ),
          );
          return (
            <li key={e.date}>
              <button
                type="button"
                className={
                  "date-button" +
                  (e.date === selectedDate && selectedSession === null
                    ? " date-button-selected"
                    : "")
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
              {(sortedLabelled.length > 0 || legacySessions.length > 1) && (
                <ul className="date-session-list">
                  {legacySessions.length > 1 &&
                    legacySessions.map((s) => (
                      <li key={s.modeFolder}>
                        <button
                          type="button"
                          className={
                            "session-button" +
                            (selectedSession?.date === e.date &&
                            selectedSession?.modeFolder === s.modeFolder
                              ? " session-button-selected"
                              : "")
                          }
                          onClick={() => onSelectSession(e.date, s.modeFolder)}
                        >
                          <span className="session-label">
                            {s.modeFolder}
                          </span>
                          <span className="session-mode-tag">{s.mode}</span>
                        </button>
                      </li>
                    ))}
                  {sortedLabelled.map((s) => (
                    <li key={s.modeFolder}>
                      <button
                        type="button"
                        className={
                          "session-button" +
                          (selectedSession?.date === e.date &&
                          selectedSession?.modeFolder === s.modeFolder
                            ? " session-button-selected"
                            : "")
                        }
                        onClick={() => onSelectSession(e.date, s.modeFolder)}
                      >
                        <span className="session-label">
                          {s.configLabel ?? s.modeFolder}
                        </span>
                        <span className="session-mode-tag">{s.mode}</span>
                      </button>
                    </li>
                  ))}
                </ul>
              )}
            </li>
          );
        })}
      </ul>
    </aside>
  );
};
