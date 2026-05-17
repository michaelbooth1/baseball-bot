import sys
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[1]
MONITOR_DIR = PROJECT_DIR / "scripts" / "monitor"
if str(MONITOR_DIR) not in sys.path:
    sys.path.insert(0, str(MONITOR_DIR))

from monitor_cli import parse_args  # noqa: E402


def _parse_monitor_args(argv):
    old_argv = sys.argv
    sys.argv = ["monitor_mlb_polymarket_ou.py", *argv]
    try:
        return parse_args()
    finally:
        sys.argv = old_argv


def test_performance_mode_defaults_on():
    args = _parse_monitor_args([])

    assert args.performance_mode is True


def test_no_performance_mode_disables_default():
    args = _parse_monitor_args(["--no-performance-mode"])

    assert args.performance_mode is False


def test_performance_mode_flag_remains_backward_compatible():
    args = _parse_monitor_args(["--performance-mode"])

    assert args.performance_mode is True
