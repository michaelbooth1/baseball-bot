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
 *   GET /api/sessions                      -> { sessions: [{date, mode, modeFolder, configLabel?}, ...] }
 *   GET /api/sessions/<modeFolder>/<date>  -> session JSON
 *   GET /api/parallel-comparisons          -> { ranges: ["2026-05-24_2026-05-24", ...] }
 *   GET /api/parallel-comparisons/<range>  -> aggregator JSON for that date range
 *
 * 2026-05-25: extended to auto-discover any `data/paper_*` directory
 * (e.g., `paper_A_current`, `paper_B_cal_only`) so multi-engine
 * runs surface in the sidebar alongside the legacy `paper_trading`
 * and `live_trading` folders. URL slugs map 1:1 to dir names except:
 *   - `live` -> data/live_trading/sessions/  (legacy alias)
 *   - `paper` -> data/paper_trading/sessions/ (legacy alias)
 *   - `paper_<X>` -> data/paper_<X>/sessions/ (X = launcher label)
 */
function dailyReviewApiPlugin(): Plugin {
  const projectRoot = path.resolve(__dirname, "..");
  const dataRoot = path.join(projectRoot, "data");
  const reviewsDir = path.join(
    dataRoot,
    "analysis_output",
    "daily_human_review",
  );
  const parallelDir = path.join(
    dataRoot,
    "analysis_output",
    "parallel_engine_comparison",
  );
  const sessionFileRe = /^(\d{4}-\d{2}-\d{2})_session\.json$/;
  // Mirrors launch_parallel_engines.LABEL_RE: first char letter/digit,
  // then up to 63 more of letter/digit/dot/dash/underscore.
  const labelCharRe = /^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$/;
  const comparisonRangeRe = /^(\d{4}-\d{2}-\d{2})_(\d{4}-\d{2}-\d{2})$/;

  /** Map a URL slug to its filesystem directory, or null if invalid/unsafe. */
  function resolveSessionDir(slug: string): string | null {
    if (slug === "live") {
      return path.join(dataRoot, "live_trading", "sessions");
    }
    if (slug === "paper") {
      return path.join(dataRoot, "paper_trading", "sessions");
    }
    if (slug.startsWith("paper_")) {
      const label = slug.slice("paper_".length);
      if (!labelCharRe.test(label)) return null;
      return path.join(dataRoot, slug, "sessions");
    }
    return null;
  }

  /** List every session-bearing directory we know about, plus the URL slug
   *  the client should use for it. Auto-discovers `paper_*` subdirs of `data/`
   *  so multi-engine runs show up without config changes. */
  async function discoverSessionFolders(): Promise<
    Array<{ slug: string; dir: string }>
  > {
    const folders: Array<{ slug: string; dir: string }> = [];
    folders.push({ slug: "live", dir: path.join(dataRoot, "live_trading", "sessions") });
    folders.push({ slug: "paper", dir: path.join(dataRoot, "paper_trading", "sessions") });
    try {
      const entries = await fs.readdir(dataRoot, { withFileTypes: true });
      for (const e of entries) {
        if (!e.isDirectory()) continue;
        if (!e.name.startsWith("paper_")) continue;
        if (e.name === "paper_trading") continue;
        const label = e.name.slice("paper_".length);
        if (!labelCharRe.test(label)) continue;
        folders.push({
          slug: e.name,
          dir: path.join(dataRoot, e.name, "sessions"),
        });
      }
    } catch {
      // data/ unreadable - fall through with legacy folders only
    }
    return folders;
  }

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

        // GET /api/sessions -> list of (date, modeFolder, mode, configLabel?)
        if (req.url === "/api/sessions") {
          const sessions: Array<{
            date: string;
            mode: string;
            modeFolder: string;
            configLabel?: string;
          }> = [];
          const errors: Array<{ modeFolder: string; detail: string }> = [];
          const folders = await discoverSessionFolders();
          for (const { slug, dir } of folders) {
            try {
              const entries = await fs.readdir(dir);
              for (const f of entries) {
                const m = sessionFileRe.exec(f);
                if (!m) continue;
                const date = m[1];
                let mode = slug;
                let configLabel: string | undefined;
                try {
                  const body = await fs.readFile(path.join(dir, f), "utf8");
                  const parsed = JSON.parse(body) as {
                    mode?: string;
                    params?: { config_label?: string };
                  };
                  if (typeof parsed.mode === "string") mode = parsed.mode;
                  if (typeof parsed.params?.config_label === "string") {
                    configLabel = parsed.params.config_label;
                  }
                } catch {
                  // keep slug as mode fallback
                }
                sessions.push({ date, mode, modeFolder: slug, configLabel });
              }
            } catch (err) {
              errors.push({ modeFolder: slug, detail: String(err) });
            }
          }
          sessions.sort((a, b) => {
            if (a.date !== b.date) return a.date.localeCompare(b.date);
            return a.modeFolder.localeCompare(b.modeFolder);
          });
          res.setHeader("Content-Type", "application/json");
          res.end(JSON.stringify({ sessions, errors }));
          return;
        }

        // GET /api/sessions/<modeFolder>/<date>
        // modeFolder slug accepts uppercase + digits for paper_<label> dirs.
        const sessionMatch = req.url.match(
          /^\/api\/sessions\/([A-Za-z0-9_.-]+)\/(\d{4}-\d{2}-\d{2})$/,
        );
        if (sessionMatch) {
          const modeFolder = sessionMatch[1];
          const date = sessionMatch[2];
          const dir = resolveSessionDir(modeFolder);
          if (!dir) {
            res.statusCode = 400;
            res.setHeader("Content-Type", "application/json");
            res.end(
              JSON.stringify({
                error: "unknown-mode-folder",
                modeFolder,
                hint:
                  "Allowed slugs: 'live', 'paper', or 'paper_<label>' " +
                  "where label matches the launcher LABEL_RE.",
              }),
            );
            return;
          }
          const filePath = path.join(dir, `${date}_session.json`);
          try {
            const body = await fs.readFile(filePath, "utf8");
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
              // raw body
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

        // GET /api/parallel-comparisons -> available date ranges
        if (req.url === "/api/parallel-comparisons") {
          try {
            const entries = await fs.readdir(parallelDir);
            const ranges = entries
              .filter(
                (f) =>
                  f.startsWith("parallel_engine_comparison_") &&
                  f.endsWith(".json"),
              )
              .map((f) =>
                f
                  .replace("parallel_engine_comparison_", "")
                  .replace(".json", ""),
              )
              .filter((r) => comparisonRangeRe.test(r))
              .sort();
            res.setHeader("Content-Type", "application/json");
            res.end(JSON.stringify({ ranges, parallelDir }));
          } catch (err) {
            res.statusCode = 404;
            res.setHeader("Content-Type", "application/json");
            res.end(
              JSON.stringify({
                error: "parallel-comparisons-dir-unreadable",
                parallelDir,
                detail: String(err),
              }),
            );
          }
          return;
        }

        // GET /api/parallel-comparisons/<start>_<end>
        const compMatch = req.url.match(
          /^\/api\/parallel-comparisons\/(\d{4}-\d{2}-\d{2}_\d{4}-\d{2}-\d{2})$/,
        );
        if (compMatch) {
          const range = compMatch[1];
          if (!comparisonRangeRe.test(range)) {
            res.statusCode = 400;
            res.setHeader("Content-Type", "application/json");
            res.end(JSON.stringify({ error: "invalid-range-format", range }));
            return;
          }
          const filePath = path.join(
            parallelDir,
            `parallel_engine_comparison_${range}.json`,
          );
          try {
            const body = await fs.readFile(filePath, "utf8");
            res.setHeader("Content-Type", "application/json");
            res.end(body);
          } catch (err) {
            res.statusCode = 404;
            res.setHeader("Content-Type", "application/json");
            res.end(
              JSON.stringify({
                error: "parallel-comparison-not-found",
                range,
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
