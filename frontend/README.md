# MLB Polymarket Bot — Frontend (daily-review viewer)

A small Vite + React + TypeScript app that turns the daily-review
JSON artifacts (produced by
`scripts/analysis/build_daily_human_review_report.py`) into a
browsable web UI.

## What it shows

For each day's session:
- **Session summary** — mode, bets placed / filled, profit, ROI, win rate.
- **Notes panel** — the mirrored alert lines (`Cohort-roi:`,
  `Stage1-shadow:`, `Under-outcomes:`, etc.), color-coded by
  severity prefix.
- **Progress milestones** — visual bars showing how close the bot
  is to the 3 active data-accumulation gates:
  - Active #1 walk-forward: filled bets + session dates toward
    150 / 30
  - Phase A5 → B4: UNDER session count toward 60
  - Stage-1 Alt-A: trailing-30d settled bets toward 150
- **Bets table** — per-bet detail for the selected session.
- **Health blocks grid** — every health block as a status card
  colored by alert count (green / yellow / red); click to expand
  the raw alert list.

The progress milestones panel aggregates the trailing 14 dates
(configurable in `App.tsx`); everything else is per-selected-day.

## How to run

Prereqs: Node 18+ and npm (or pnpm / yarn).

```bash
cd frontend
npm install
npm run dev
```

The dev server starts on `http://localhost:5173`. It includes a
custom middleware that reads the daily-review JSONs directly from
`../data/analysis_output/daily_human_review/` — no copy step
required. New reviews from `run_daily_refresh.py` show up on the
next page reload.

For a production-style static build:
```bash
npm run build
npm run preview
```

(Note: the static build does NOT bundle the daily-review JSONs;
the dev middleware is dev-only. A production deploy would need a
sibling API server or a build step that copies the JSONs into
`dist/`. v1 is intentionally dev-only.)

## File layout

```
frontend/
├── README.md
├── package.json
├── tsconfig.json
├── vite.config.ts        # dev middleware that exposes /api/reviews
├── index.html
└── src/
    ├── main.tsx          # React entry
    ├── App.tsx           # composes the page from components
    ├── App.css           # all styling (no external CSS framework)
    ├── api.ts            # fetch helpers + number formatters
    ├── types.ts          # TypeScript shape of the daily-review JSON
    └── components/
        ├── DateSidebar.tsx
        ├── SessionSummary.tsx
        ├── NotesPanel.tsx
        ├── ProgressMilestones.tsx
        ├── BetsTable.tsx
        └── HealthStatusGrid.tsx
```

## Extending

- New health block from the analysis layer? Add it to the
  `BLOCK_ORDER` list in `HealthStatusGrid.tsx` and to the
  `DailyReview` type in `types.ts`. No other code changes needed
  — the grid renders any block with a `.alerts: string[]` surface.
- Want charts (per-day P&L line, cohort heatmap, etc.)? Add
  `recharts` to `package.json` and render alongside the existing
  components. Per-day arrays are available via the trailing
  reviews already loaded by `App.tsx`.
- Per-bet drill-down? `BetsTable.tsx` is the entry point; each
  row currently shows a compact summary. Wrap in `<details>` or
  link to a `/bet/<bet_id>` route (would need react-router).

## Why these tech choices

- **Vite over Next.js**: zero backend; we only need a dev server
  with hot reload and a small middleware to expose local JSON.
- **React over Svelte/Vue**: most-likely operator familiarity +
  the largest component ecosystem for the eventual chart upgrade.
- **TypeScript**: the daily-review JSON shape is rich (17+ health
  blocks); TS catches the schema-drift bugs that bit the unified
  signal table.
- **Plain CSS over Tailwind**: a single 250-LOC `App.css` is
  faster to scan than utility classes for v1, and adding Tailwind
  would mean a build-step refactor down the line.
