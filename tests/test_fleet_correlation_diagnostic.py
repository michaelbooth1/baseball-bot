"""Tests for scripts/analysis/fleet_correlation_diagnostic.py."""
import datetime as _dt
import json
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parents[1]
ANALYSIS_DIR = PROJECT_DIR / "scripts" / "analysis"
if str(ANALYSIS_DIR) not in sys.path:
    sys.path.insert(0, str(ANALYSIS_DIR))

import fleet_correlation_diagnostic as fcd  # noqa: E402


def _make_session(
    *,
    date: str,
    bets: list,
) -> dict:
    """Minimal session.json shape covering the fields the script reads."""
    n_bets = len(bets)
    wins = sum(1 for b in bets if b.get("won") is True)
    losses = sum(1 for b in bets if b.get("won") is False)
    return {
        "date": date,
        "mode": "paper",
        "summary": {
            "total_bets": n_bets,
            "settled": n_bets,
            "wins": wins,
            "losses": losses,
        },
        "bets": bets,
    }


def _write_paper_session(
    paper_prefix: Path,
    *,
    label: str,
    date: str,
    bets: list,
) -> Path:
    sess_dir = paper_prefix / f"paper_{label}" / "sessions"
    sess_dir.mkdir(parents=True, exist_ok=True)
    path = sess_dir / f"{date}_session.json"
    path.write_text(json.dumps(_make_session(date=date, bets=bets)), encoding="utf-8")
    return path


def _write_aggregator(
    aggregator_dir: Path,
    *,
    date: str,
    counts: dict,
) -> Path:
    aggregator_dir.mkdir(parents=True, exist_ok=True)
    path = aggregator_dir / f"parallel_engine_comparison_{date}_{date}.json"
    path.write_text(
        json.dumps({
            "date_range": {"start": date, "end": date},
            "shared_candidate_disagreement": {
                "game_line": {"counts": counts},
            },
        }),
        encoding="utf-8",
    )
    return path


class CorrelatedLossClusteringTests(unittest.TestCase):
    """Verify the cluster math: when N models lose on the same (game, line, side),
    max_correlated_loss_share = N / n_active_presets."""

    def test_unanimous_loss_on_single_game(self):
        """12 of 12 active presets lose on the same bet -> 100% concentration."""
        sessions = {}
        for i in range(12):
            label = f"preset_{i}"
            sessions[label] = _make_session(
                date="2026-05-27",
                bets=[{
                    "game_pk": 824270, "line": "4.5", "side": "over",
                    "won": False, "profit": -10.0,
                }],
            )
        corr = fcd._compute_correlated_loss(sessions)
        self.assertEqual(corr["max_correlated_loss_share"], 1.0)
        self.assertEqual(corr["max_correlated_loss_n"], 12)
        self.assertEqual(corr["n_unique_loser_keys"], 1)
        self.assertEqual(corr["n_active_presets"], 12)

    def test_losses_spread_across_games(self):
        """Each loss on a different (game, line, side) -> share = 1/N."""
        sessions = {}
        for i in range(5):
            sessions[f"preset_{i}"] = _make_session(
                date="2026-05-31",
                bets=[{
                    "game_pk": 10000 + i, "line": "7.5", "side": "over",
                    "won": False, "profit": -10.0,
                }],
            )
        corr = fcd._compute_correlated_loss(sessions)
        self.assertEqual(corr["max_correlated_loss_n"], 1)
        self.assertEqual(corr["n_unique_loser_keys"], 5)
        self.assertAlmostEqual(corr["max_correlated_loss_share"], 1 / 5)

    def test_no_losses_zero_concentration(self):
        sessions = {}
        for i in range(4):
            sessions[f"preset_{i}"] = _make_session(
                date="2026-05-30",
                bets=[{
                    "game_pk": 100, "line": "8.5", "side": "over",
                    "won": True, "profit": +5.0,
                }],
            )
        corr = fcd._compute_correlated_loss(sessions)
        self.assertEqual(corr["max_correlated_loss_share"], 0.0)
        self.assertEqual(corr["max_correlated_loss_n"], 0)
        self.assertEqual(corr["n_unique_loser_keys"], 0)
        self.assertIsNone(corr["max_correlated_loss_key"])


class EndToEndTests(unittest.TestCase):
    """The full pipeline: scan paper_* dirs + aggregator JSONs, write output."""

    def test_main_emits_json_and_markdown(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            paper_prefix = tmp / "data"
            aggregator_dir = tmp / "agg"
            output_dir = tmp / "out"

            today = _dt.date.today().isoformat()

            # 3 presets, all of which lose on the same game-line.
            for label in ("X1", "X2", "X3"):
                _write_paper_session(
                    paper_prefix, label=label, date=today,
                    bets=[{
                        "game_pk": 99999, "line": "7.5", "side": "over",
                        "won": False, "profit": -10.0,
                    }],
                )

            _write_aggregator(
                aggregator_dir,
                date=today,
                counts={
                    "keys_compared": 10,
                    "split": 1,
                    "unanimous_trade": 1,
                    "unanimous_skip": 8,
                },
            )

            rc = fcd.main([
                "--paper-prefix", str(paper_prefix),
                "--aggregator-dir", str(aggregator_dir),
                "--output-dir", str(output_dir),
                "--recent-days", "1",
                "--today", today,
                "--warn-threshold", "0.5",
            ])
            self.assertEqual(rc, 0)
            payload = json.loads((output_dir / "fleet_correlation_diagnostic.json").read_text(encoding="utf-8"))
            self.assertEqual(payload["days_scanned"], 1)
            self.assertEqual(payload["days_flagged"], 1)  # 3/3 = 100% > 50% threshold
            [f] = payload["findings"]
            self.assertEqual(f["date"], today)
            self.assertEqual(f["n_presets_active"], 3)
            self.assertEqual(f["max_correlated_loss_share"], 1.0)
            self.assertEqual(f["max_correlated_loss_n"], 3)
            self.assertAlmostEqual(f["split_density"], 0.1)
            self.assertEqual(f["n_unanimous_trade"], 1)
            md = (output_dir / "fleet_correlation_diagnostic.md").read_text(encoding="utf-8")
            self.assertIn("Fleet Correlation Diagnostic", md)
            self.assertIn(today, md)

    def test_main_handles_missing_aggregator(self):
        """If no aggregator JSON exists for a date, split_density falls back to 0
        but correlated-loss metrics still compute from sessions."""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            paper_prefix = tmp / "data"
            aggregator_dir = tmp / "agg_does_not_exist"
            output_dir = tmp / "out"
            today = _dt.date.today().isoformat()
            for label in ("X1", "X2"):
                _write_paper_session(
                    paper_prefix, label=label, date=today,
                    bets=[{
                        "game_pk": 1, "line": "7.5", "side": "over",
                        "won": True, "profit": +5.0,
                    }],
                )
            rc = fcd.main([
                "--paper-prefix", str(paper_prefix),
                "--aggregator-dir", str(aggregator_dir),
                "--output-dir", str(output_dir),
                "--recent-days", "1",
                "--today", today,
            ])
            self.assertEqual(rc, 0)
            payload = json.loads((output_dir / "fleet_correlation_diagnostic.json").read_text(encoding="utf-8"))
            [f] = payload["findings"]
            self.assertEqual(f["split_density"], 0.0)
            self.assertEqual(f["keys_compared"], 0)
            self.assertEqual(f["n_paper_bets_fleet"], 2)

    def test_main_no_sessions_returns_zero_findings(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            output_dir = tmp / "out"
            rc = fcd.main([
                "--paper-prefix", str(tmp / "data"),
                "--aggregator-dir", str(tmp / "agg"),
                "--output-dir", str(output_dir),
                "--recent-days", "3",
            ])
            self.assertEqual(rc, 0)
            payload = json.loads((output_dir / "fleet_correlation_diagnostic.json").read_text(encoding="utf-8"))
            self.assertEqual(payload["days_scanned"], 0)
            self.assertEqual(payload["findings"], [])


if __name__ == "__main__":
    unittest.main()
