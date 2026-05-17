import json
import sys
import tempfile
import unittest
from collections import Counter
from pathlib import Path
from types import SimpleNamespace


PROJECT_DIR = Path(__file__).resolve().parents[1]
MONITOR_DIR = PROJECT_DIR / "scripts" / "monitor"
TRADING_DIR = PROJECT_DIR / "scripts" / "trading"
for path in (MONITOR_DIR, TRADING_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import monitor_mlb_polymarket_ou as monitor  # noqa: E402
import signal_engine as se  # noqa: E402


def _game(game_pk: int = 1) -> monitor.ScheduledGame:
    return monitor.ScheduledGame(
        game_pk=game_pk,
        game_date="2026-04-30",
        start_time_utc="2026-04-30T19:00:00Z",
        away_abbrev="SF",
        home_abbrev="PHI",
        away_name="San Francisco Giants",
        home_name="Philadelphia Phillies",
        status_abstract="Live",
        status_detailed="In Progress",
        score=monitor.ScheduleScore(
            away=1,
            home=1,
            inning=5,
            inning_state="Top",
            outs=1,
            balls=0,
            strikes=0,
            runners_on=0,
        ),
    )


class _FakeStatsClient:
    def __init__(self, games):
        self.games = games

    def fetch_schedule_payload(self, date_str):
        return {"date": date_str}

    def parse_games(self, payload):
        return self.games


class _SequenceStatsClient:
    def __init__(self, game_sets):
        self.game_sets = list(game_sets)
        self.idx = 0

    def fetch_schedule_payload(self, date_str):
        return {"date": date_str}

    def parse_games(self, payload):
        out = self.game_sets[min(self.idx, len(self.game_sets) - 1)]
        self.idx += 1
        return out


class _FakeRecorder:
    def __init__(self):
        self.writes = 0

    def write_schedule(self, payload):
        self.writes += 1


class ObservabilityNoiseRollupTests(unittest.TestCase):
    def test_repeated_successful_schedule_refreshes_are_compacted_after_first_info(self):
        engine = monitor.MLBPolymarketMonitor.__new__(monitor.MLBPolymarketMonitor)
        engine.date_str = "2026-04-30"
        engine.stats_client = _FakeStatsClient({1: _game(1)})
        engine.recorder = _FakeRecorder()
        engine.games = {}
        engine._schedule_refresh_count = 0
        engine._schedule_refresh_info_count = 0
        engine._schedule_refresh_change_count = 0
        engine._schedule_refresh_error_count = 0
        engine._last_schedule_signature = None

        with self.assertLogs("mlb_poly_monitor", level="DEBUG") as captured:
            engine._refresh_schedule()
            engine._refresh_schedule()

        info_lines = [line for line in captured.output if line.startswith("INFO:")]
        debug_lines = [line for line in captured.output if line.startswith("DEBUG:")]
        self.assertEqual(len(info_lines), 1)
        self.assertEqual(len(debug_lines), 0)
        self.assertEqual(engine._schedule_refresh_count, 2)

    def test_schedule_state_changes_are_periodic_rollups_not_per_refresh(self):
        game_sets = []
        for idx in range(monitor.SCHEDULE_CHANGED_DEBUG_EVERY_N_REFRESHES + 2):
            game = _game(1)
            game.score.away = idx % 7
            game.score.home = (idx // 2) % 5
            game.score.inning = 4 + (idx % 5)
            game_sets.append({1: game})

        engine = monitor.MLBPolymarketMonitor.__new__(monitor.MLBPolymarketMonitor)
        engine.date_str = "2026-04-30"
        engine.stats_client = _SequenceStatsClient(game_sets)
        engine.recorder = _FakeRecorder()
        engine.games = {}
        engine._schedule_refresh_count = 0
        engine._schedule_refresh_info_count = 0
        engine._schedule_refresh_change_count = 0
        engine._schedule_refresh_change_since_log = 0
        engine._schedule_refresh_change_rollup = Counter()
        engine._schedule_refresh_error_count = 0
        engine._last_schedule_signature = None

        with self.assertLogs("mlb_poly_monitor", level="DEBUG") as captured:
            for _ in game_sets:
                engine._refresh_schedule()

        info_lines = [line for line in captured.output if line.startswith("INFO:")]
        debug_lines = [line for line in captured.output if line.startswith("DEBUG:")]
        self.assertEqual(len(info_lines), 1)
        self.assertEqual(len(debug_lines), 2)
        self.assertTrue(all("Schedule state-change rollup" in line for line in debug_lines))
        self.assertEqual(
            engine._schedule_refresh_change_count,
            monitor.SCHEDULE_CHANGED_DEBUG_EVERY_N_REFRESHES + 1,
        )
        self.assertEqual(sum(engine._schedule_refresh_change_rollup.values()), engine._schedule_refresh_change_count)

    def test_poll_progress_debug_is_periodic_not_per_cycle(self):
        engine = monitor.MLBPolymarketMonitor.__new__(monitor.MLBPolymarketMonitor)
        engine._poll_cycles = 1
        self.assertTrue(engine._should_log_poll_progress_debug())
        engine._poll_cycles = 2
        self.assertFalse(engine._should_log_poll_progress_debug())
        engine._poll_cycles = monitor.POLL_PROGRESS_DEBUG_EVERY_N_CYCLES
        self.assertTrue(engine._should_log_poll_progress_debug())

    def test_market_discovery_no_new_mapping_info_is_rolled_up(self):
        engine = monitor.MLBPolymarketMonitor.__new__(monitor.MLBPolymarketMonitor)
        engine._market_discovery_no_new_cycles = 0
        engine._market_discovery_no_new_attempted_games = 0
        engine._market_discovery_no_new_since_log = 0
        engine._market_discovery_no_new_attempted_since_log = 0
        engine._market_discovery_no_new_info_every_n_cycles = 2

        with self.assertLogs("mlb_poly_monitor", level="DEBUG") as captured:
            engine._record_market_discovery_no_new(2)
            engine._record_market_discovery_no_new(3)
            engine._record_market_discovery_no_new(4)

        info_lines = [line for line in captured.output if line.startswith("INFO:")]
        debug_lines = [line for line in captured.output if line.startswith("DEBUG:")]
        self.assertEqual(len(info_lines), 2)
        self.assertEqual(len(debug_lines), 1)
        self.assertTrue(all("Market discovery no-new-mapping rollup" in line for line in info_lines))
        self.assertIn("no new mappings this cycle", debug_lines[0])
        self.assertEqual(engine._market_discovery_no_new_cycles, 3)
        self.assertEqual(engine._market_discovery_no_new_attempted_games, 9)

    def test_noisy_library_loggers_are_suppressed_to_warning(self):
        logger_names = ["urllib3.connectionpool", "httpcore.http2", "hpack.hpack"]
        old_levels = {name: monitor.logging.getLogger(name).level for name in logger_names}
        try:
            for name in logger_names:
                monitor.logging.getLogger(name).setLevel(monitor.logging.DEBUG)
            monitor.suppress_noisy_library_loggers()
            for name in logger_names:
                self.assertGreaterEqual(
                    monitor.logging.getLogger(name).getEffectiveLevel(),
                    monitor.logging.WARNING,
                )
        finally:
            for name, level in old_levels.items():
                monitor.logging.getLogger(name).setLevel(level)

    def test_repeated_book_retirement_logs_info_once_then_debug_and_rolls_up(self):
        engine = monitor.MLBPolymarketMonitor.__new__(monitor.MLBPolymarketMonitor)
        engine.args = SimpleNamespace(
            book_failure_retire_streak=1,
            book_failure_cooldown_secs=120.0,
            book_failure_max_cooldown_secs=1800.0,
        )
        engine._book_fail_streak = {}
        engine._book_fail_cooldown_until = {}
        engine._book_fail_retire_count = {}
        engine._book_fail_retire_log_count = {}
        engine._book_fail_retire_rollup = {}
        key = (1, "8.5", "over_yes")
        game = _game(1)
        result = {"source": "clob", "status_code": 404, "error": "http_404"}

        with self.assertLogs("mlb_poly_monitor", level="DEBUG") as captured:
            engine._record_retirable_book_failure(
                key=key,
                game=game,
                line="8.5",
                side="over_yes",
                token_id="token123456789",
                result=result,
                now=1000.0,
            )
            engine._record_retirable_book_failure(
                key=key,
                game=game,
                line="8.5",
                side="over_yes",
                token_id="token123456789",
                result=result,
                now=1125.0,
            )

        self.assertEqual(sum(line.startswith("INFO:") for line in captured.output), 1)
        self.assertEqual(sum(line.startswith("DEBUG:") for line in captured.output), 1)
        self.assertEqual(engine._book_fail_retire_rollup[key]["retire_count"], 2)
        self.assertEqual(engine._book_fail_retire_log_count[key], 2)

    def test_candidate_rollup_preserves_raw_rows_and_counts_dedup_suppression(self):
        engine = se.SignalEngine.__new__(se.SignalEngine)
        engine.date_str = "2026-04-30"
        engine._candidate_mode = "paper"
        engine._candidate_rows_written = 0
        engine._candidate_rows_dedup_suppressed = 0
        engine._candidate_rows_write_errors = 0
        engine._candidate_null_fields_omitted = 0
        engine._candidate_skip_dedup_seen = set()
        engine._candidate_rollup_by_write_status = Counter()
        engine._candidate_rollup_by_decision = Counter()
        engine._candidate_rollup_by_reason = Counter()
        engine._candidate_rollup_by_strategy = Counter()
        engine._candidate_rollup_by_game_line_reason = Counter()

        with tempfile.TemporaryDirectory() as td:
            engine.trade_args = SimpleNamespace(paper_root=Path(td))
            payload = {
                "candidate_id": "c1",
                "game_pk": 1,
                "away_abbrev": "SF",
                "home_abbrev": "PHI",
                "line": "8.5",
                "inning": 5,
                "inning_state": "Top",
                "outs": 1,
                "current_total": 2,
                "away_score_before": 1,
                "home_score_before": 1,
                "runners_on": 0,
                "decision": "skip",
                "decision_reason": "gate_min_edge",
                "decision_ask": 0.62,
                "best_bid": 0.61,
                "edge": 0.08,
                "fair_value": 0.70,
            }

            engine._record_candidate_decision(payload)
            engine._record_candidate_decision(dict(payload, candidate_id="c2"))
            engine._write_candidate_rollup()

            raw_rows = engine._candidate_log_path().read_text(encoding="utf-8").splitlines()
            rollup = json.loads(engine._candidate_rollup_path().read_text(encoding="utf-8"))

        self.assertEqual(len(raw_rows), 1)
        self.assertEqual(rollup["attempted_rows"], 2)
        self.assertEqual(rollup["written_rows"], 1)
        self.assertEqual(rollup["dedup_suppressed_rows"], 1)
        self.assertEqual(rollup["by_write_status"]["written"], 1)
        self.assertEqual(rollup["by_write_status"]["dedup_suppressed"], 1)
        self.assertEqual(rollup["by_decision_reason"]["skip:gate_min_edge"], 2)


if __name__ == "__main__":
    unittest.main()
