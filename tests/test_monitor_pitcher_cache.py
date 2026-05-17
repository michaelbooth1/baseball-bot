import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[1]
MONITOR_DIR = PROJECT_DIR / "scripts" / "monitor"
if str(MONITOR_DIR) not in sys.path:
    sys.path.insert(0, str(MONITOR_DIR))

import monitor_mlb_polymarket_ou as monitor  # noqa: E402


def _write_cache(path: Path, *, built_at: str, pitcher_count: int = 80) -> None:
    pitchers = {
        str(i): {"name": f"Pitcher {i}", "era": 3.5, "ip": 10.0, "gs": 0}
        for i in range(pitcher_count)
    }
    path.write_text(
        json.dumps(
            {
                "built_at": built_at,
                "season": datetime.now(timezone.utc).year,
                "pitcher_count": pitcher_count,
                "pitchers": pitchers,
            }
        ),
        encoding="utf-8",
    )


class _FakeStatsClient(monitor.MLBStatsClient):
    def __init__(self) -> None:
        self.build_calls = 0
        self._pitcher_cache = {}

    def _build_pitcher_cache(self, path: Path) -> bool:
        self.build_calls += 1
        _write_cache(path, built_at="2026-04-30T12:00:00+00:00", pitcher_count=80)
        return True


class MonitorPitcherCacheTests(unittest.TestCase):
    def test_pitcher_cache_rebuilds_once_per_refresh_date(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "pitcher_cache.json"
            _write_cache(path, built_at="2026-04-29T12:00:00+00:00", pitcher_count=80)
            client = _FakeStatsClient()

            client._load_or_rebuild_pitcher_cache(path, refresh_date="2026-04-30")

            self.assertEqual(client.build_calls, 1)
            self.assertEqual(len(client._pitcher_cache), 80)

    def test_pitcher_cache_same_refresh_date_does_not_rebuild(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "pitcher_cache.json"
            _write_cache(path, built_at="2026-04-30T12:00:00+00:00", pitcher_count=80)
            client = _FakeStatsClient()

            client._load_or_rebuild_pitcher_cache(path, refresh_date="2026-04-30")

            self.assertEqual(client.build_calls, 0)
            self.assertEqual(len(client._pitcher_cache), 80)


if __name__ == "__main__":
    unittest.main()
