"""Tests for `_atomic_replace_with_retry` in scrape_mlb_history.

2026-06-04 fix: the `scrape_active_schedule` refresh step had been
failing on Windows with PermissionError (WinError 32) when the live
engine tailed the schedule file during the daily refresh. Tests
exercise the retry-with-backoff helper that replaced the bare
`tmp.replace(path)` call.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

PROJECT_DIR = Path(__file__).resolve().parents[1]
SCRAPING_DIR = PROJECT_DIR / "scripts" / "scraping"
if str(SCRAPING_DIR) not in sys.path:
    sys.path.insert(0, str(SCRAPING_DIR))

import scrape_mlb_history as smh  # noqa: E402


class AtomicReplaceWithRetryTests(unittest.TestCase):

    def test_success_on_first_try_does_not_sleep(self):
        """Happy path: target lock is clear, rename succeeds first
        attempt, no sleep call. Verifies we don't pay latency in the
        common case."""
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "src.json"
            dst = Path(td) / "dst.json"
            src.write_text("payload", encoding="utf-8")
            with patch.object(smh.time, "sleep") as mock_sleep:
                smh._atomic_replace_with_retry(src, dst)
            self.assertFalse(src.exists())
            self.assertTrue(dst.exists())
            self.assertEqual(dst.read_text(), "payload")
            mock_sleep.assert_not_called()

    def test_retries_on_permission_error_then_succeeds(self):
        """When the first replace attempt fails with PermissionError
        (Windows lock), the helper sleeps + retries. Verify success
        after one transient failure."""
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "src.json"
            dst = Path(td) / "dst.json"
            src.write_text("payload", encoding="utf-8")

            real_replace = Path.replace
            call_state = {"count": 0}

            def flaky_replace(self_p, target):
                call_state["count"] += 1
                if call_state["count"] == 1:
                    raise PermissionError(
                        32,
                        "The process cannot access the file because "
                        "it is being used by another process",
                        str(target),
                    )
                return real_replace(self_p, target)

            with patch.object(Path, "replace", flaky_replace), \
                 patch.object(smh.time, "sleep") as mock_sleep:
                smh._atomic_replace_with_retry(src, dst)

            self.assertEqual(call_state["count"], 2)
            self.assertTrue(dst.exists())
            mock_sleep.assert_called_once()
            # First backoff = base_delay_s * 2**0 = 0.1
            self.assertAlmostEqual(
                mock_sleep.call_args[0][0], 0.1, places=4,
            )

    def test_exhausted_retries_reraises_last_error(self):
        """When all attempts fail, re-raise the original
        PermissionError so the caller's error handling (refresh
        step's failure logging) sees the same exception as before."""
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "src.json"
            dst = Path(td) / "dst.json"
            src.write_text("payload", encoding="utf-8")

            def always_fails(self_p, target):
                raise PermissionError(
                    32, "permanent lock", str(target),
                )

            with patch.object(Path, "replace", always_fails), \
                 patch.object(smh.time, "sleep"):
                with self.assertRaises(PermissionError) as ctx:
                    smh._atomic_replace_with_retry(src, dst)
                self.assertIn("permanent lock", str(ctx.exception))

    def test_exponential_backoff_sleeps(self):
        """Verify the backoff schedule across max_attempts-1 sleeps:
        0.1, 0.2, 0.4, 0.8 (4 sleeps for the 5 attempt default)."""
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "src.json"
            dst = Path(td) / "dst.json"
            src.write_text("payload", encoding="utf-8")

            def always_fails(self_p, target):
                raise PermissionError(32, "lock", str(target))

            with patch.object(Path, "replace", always_fails), \
                 patch.object(smh.time, "sleep") as mock_sleep:
                with self.assertRaises(PermissionError):
                    smh._atomic_replace_with_retry(src, dst)

            # 5 attempts default -> 4 sleeps with exponential backoff
            self.assertEqual(mock_sleep.call_count, 4)
            actual = [c[0][0] for c in mock_sleep.call_args_list]
            expected = [0.1, 0.2, 0.4, 0.8]
            for a, e in zip(actual, expected):
                self.assertAlmostEqual(a, e, places=4)

    def test_non_permission_error_not_retried(self):
        """Other errors (e.g., FileNotFoundError) should NOT be
        retried -- the helper only catches PermissionError because
        only that one has the Windows-lock recovery semantic."""
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "src.json"
            dst = Path(td) / "dst.json"
            src.write_text("payload", encoding="utf-8")

            def raises_fnf(self_p, target):
                raise FileNotFoundError(2, "missing", str(target))

            with patch.object(Path, "replace", raises_fnf), \
                 patch.object(smh.time, "sleep") as mock_sleep:
                with self.assertRaises(FileNotFoundError):
                    smh._atomic_replace_with_retry(src, dst)
            mock_sleep.assert_not_called()

    def test_save_json_uses_retry_helper(self):
        """save_json end-to-end: writes to tmp then atomically
        replaces, picking up the retry semantics for free."""
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "sub" / "file.json"
            payload = {"hello": "world", "n": 42}
            smh.save_json(path, payload)
            self.assertTrue(path.exists())
            import json as _json
            self.assertEqual(_json.loads(path.read_text()), payload)
            # No temp debris of ANY name should remain after the rename.
            self.assertEqual(list(path.parent.glob("*.tmp")), [])

    def test_concurrent_save_json_no_collision(self):
        """Regression (2026-06-15): the launcher's daily refresh and the
        dry-run engine's startup refresh both call scrape_active_schedule and
        wrote the SAME `<file>.tmp`; the loser of the atomic replace raised
        `FileNotFoundError: ...<file>.tmp`. Unique per-writer temp names
        (pid + random token) eliminate the race -- concurrent writers all
        succeed and leave no debris."""
        import json as _json
        import threading
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "2026" / "05" / "schedule.json"
            errors: list = []

            def worker():
                for _ in range(40):
                    try:
                        smh.save_json(path, {"dates": [1, 2, 3]})
                    except Exception as exc:  # noqa: BLE001
                        errors.append(repr(exc))

            threads = [threading.Thread(target=worker) for _ in range(6)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

            self.assertEqual(errors, [])
            self.assertEqual(_json.loads(path.read_text()), {"dates": [1, 2, 3]})
            self.assertEqual(list(path.parent.glob("*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
