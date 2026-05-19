import { defineConfig, type Plugin } from "vite";
import react from "@vitejs/plugin-react";
import { promises as fs } from "node:fs";
import path from "node:path";

/**
 * Custom dev middleware that exposes the project's daily-review JSON
 * artifacts to the frontend without requiring a separate backend.
 *
 *   GET /api/reviews            -> { dates: ["2026-05-17", ...] }
 *   GET /api/reviews/<date>     -> contents of <date>_human_review.json
 *
 * Reads directly from `../data/analysis_output/daily_human_review/`
 * (relative to the frontend dir). So the UI always reflects whatever
 * the daily refresh produced — no copy step required.
 *
 * Fail-soft: missing dir / missing file returns 404 with a JSON body.
 */
function dailyReviewApiPlugin(): Plugin {
  const reviewsDir = path.resolve(
    __dirname,
    "..",
    "data",
    "analysis_output",
    "daily_human_review",
  );

  return {
    name: "mlb-bot-daily-review-api",
    configureServer(server) {
      server.middlewares.use(async (req, res, next) => {
        if (!req.url || !req.url.startsWith("/api/reviews")) {
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
        const match = req.url.match(/^\/api\/reviews\/(\d{4}-\d{2}-\d{2})$/);
        if (match) {
          const date = match[1];
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
