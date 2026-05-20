import { defineConfig, type Plugin } from "vite";
import react from "@vitejs/plugin-react";
import { promises as fs } from "node:fs";
import path from "node:path";

/**
 * Custom dev middleware that exposes the project's analysis +
 * trading JSON artifacts to the frontend without requiring a
 * separate backend.
 *
 *   GET /api/reviews                       -> { dates: [...] }
 *   GET /api/reviews/<date>                -> daily_human_review JSON
 *   GET /api/sessions                      -> { sessions: [{date, mode, modeFolder}, ...] }
 *   GET /api/sessions/<modeFolder>/<date>  -> session JSON
 *
 * The daily-review artifacts are per-date and tied to ONE mode at
 * refresh time. The session JSONs always exist for BOTH paper and
 * live sessions (in their respective folders) so the WeeklyTable
 * reads sessions directly to cover all dates regardless of which
 * mode's daily review was built.
 *
 * Fail-soft: missing dir / missing file returns 404 with a JSON body.
 */
function dailyReviewApiPlugin(): Plugin {
  const projectRoot = path.resolve(__dirname, "..");
  const reviewsDir = path.join(
    projectRoot,
    "data",
    "analysis_output",
    "daily_human_review",
  );
  // Mode-folder -> sessions-subdir. The frontend addresses sessions
  // by `modeFolder` (the folder name we found them in), which is the
  // authoritative source of truth -- the session JSON's `.mode` field
  // is what gets DISPLAYED but the folder is how we LOCATE the file.
  const sessionFolders: Record<string, string> = {
    live: path.join(projectRoot, "data", "live_trading", "sessions"),
    paper: path.join(projectRoot, "data", "paper_trading", "sessions"),
  };
  const sessionFileRe = /^(\d{4}-\d{2}-\d{2})_session\.json$/;

  return {
    name: "mlb-bot-daily-review-api",
    configureServer(server) {
      server.middlewares.use(async (req, res, next) => {
        if (!req.url || !req.url.startsWith("/api/")) {
          return next();
        }

        // GET /api/reviews -> list of available dates
        if (req.url === "/api/reviews") {
          try {
            const entries = await fs.readdir(reviewsDir);
            const dates = entries
              .filter((f) => f.endsWith("_human_review.json"))
              .map((f) => f.replace("_human_review.json", ""))
              .sort();
            res.setHeader("Content-Type", "application/json");
            res.end(JSON.stringify({ dates, reviewsDir }));
          } catch (err) {
            res.statusCode = 404;
            res.setHeader("Content-Type", "application/json");
            res.end(
              JSON.stringify({
                error: "reviews-dir-unreadable",
                reviewsDir,
                detail: String(err),
              }),
            );
          }
          return;
        }

        // GET /api/reviews/<date>
        const reviewMatch = req.url.match(
          /^\/api\/reviews\/(\d{4}-\d{2}-\d{2})$/,
        );
        if (reviewMatch) {
          const date = reviewMatch[1];
          const filePath = path.join(reviewsDir, `${date}_human_review.json`);
          try {
            const body = await fs.readFile(filePath, "utf8");
            res.setHeader("Content-Type", "application/json");
            res.end(body);
          } catch (err) {
            res.statusCode = 404;
            res.setHeader("Content-Type", "application/json");
            res.end(
              JSON.stringify({
                error: "review-not-found",
                date,
                filePath,
                detail: String(err),
              }),
            );
          }
          return;
        }

        // GET /api/sessions -> list of (date, modeFolder, mode)
        // Walks BOTH live_trading + paper_trading session dirs.
        // Reads each file's `.mode` to surface the engine's
        // authoritative mode (some live folders contain dry_run
        // sessions; the folder is just a discovery hint, the
        // JSON's mode field is the truth).
        if (req.url === "/api/sessions") {
          const sessions: Array<{
            date: string;
            mode: string;
            modeFolder: string;
          }> = [];
          const errors: Array<{ modeFolder: string; detail: string }> = [];
          for (const [modeFolder, dir] of Object.entries(sessionFolders)) {
            try {
              const entries = await fs.readdir(dir);
              for (const f of entries) {
                const m = sessionFileRe.exec(f);
                if (!m) continue;
                const date = m[1];
                let mode = modeFolder; // fallback
                try {
                  const body = await fs.readFile(path.join(dir, f), "utf8");
                  const parsed = JSON.parse(body) as { mode?: string };
                  if (typeof parsed.mode === "string") mode = parsed.mode;
                } catch {
                  // unreadable -- keep modeFolder as mode fallback
                }
                sessions.push({ date, mode, modeFolder });
              }
            } catch (err) {
              // dir doesn't exist (e.g. operator has never run live);
              // skip silently
              errors.push({ modeFolder, detail: String(err) });
            }
          }
          // Sort by date then modeFolder so the response is
          // deterministic (helpful for snapshot comparisons).
          sessions.sort((a, b) => {
            if (a.date !== b.date) return a.date.localeCompare(b.date);
            return a.modeFolder.localeCompare(b.modeFolder);
          });
          res.setHeader("Content-Type", "application/json");
          res.end(JSON.stringify({ sessions, errors }));
          return;
        }

        // GET /api/sessions/<modeFolder>/<date>
        const sessionMatch = req.url.match(
          /^\/api\/sessions\/([a-z_]+)\/(\d{4}-\d{2}-\d{2})$/,
        );
        if (sessionMatch) {
          const modeFolder = sessionMatch[1];
          const date = sessionMatch[2];
          const dir = sessionFolders[modeFolder];
          if (!dir) {
            res.statusCode = 400;
            res.setHeader("Content-Type", "application/json");
            res.end(
              JSON.stringify({
                error: "unknown-mode-folder",
                modeFolder,
                allowed: Object.keys(sessionFolders),
              }),
            );
            return;
          }
          const filePath = path.join(dir, `${date}_session.json`);
          try {
            const body = await fs.readFile(filePath, "utf8");
            // Apply the same mode-fallback the index endpoint does
            // so consumers can rely on a non-null `.mode` field.
            // Older session JSONs were written without `mode` set
            // (e.g. 2026-05-18); we fall back to the folder name
            // since that's the authoritative source of truth.
            let augmentedBody = body;
            try {
              const parsed = JSON.parse(body) as {
                mode?: string | null;
                [k: string]: unknown;
              };
              if (typeof parsed.mode !== "string" || !parsed.mode) {
                parsed.mode = modeFolder;
                augmentedBody = JSON.stringify(parsed);
              }
            } catch {
              // Fall through with the raw body if the JSON is
              // unparseable (the client's .json() will surface).
            }
            res.setHeader("Content-Type", "application/json");
            res.end(augmentedBody);
          } catch (err) {
            res.statusCode = 404;
            res.setHeader("Content-Type", "application/json");
            res.end(
              JSON.stringify({
                error: "session-not-found",
                modeFolder,
                date,
                filePath,
                detail: String(err),
              }),
            );
          }
          return;
        }

        res.statusCode = 404;
        res.setHeader("Content-Type", "application/json");
        res.end(JSON.stringify({ error: "unknown-route", url: req.url }));
      });
    },
  };
}

export default defineConfig({
  plugins: [react(), dailyReviewApiPlugin()],
  server: {
    port: 5173,
    strictPort: false,
  },
});
