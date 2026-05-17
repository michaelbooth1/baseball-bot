"""Windows/Linux process tuning helpers (CPU pinning, sleep prevention).

Pulled out of the monolithic monitor module so the polling loop and
orchestration class stay focused. These are best-effort: any failure
logs a warning and continues.
"""

from __future__ import annotations

import argparse

from monitor_constants import LOGGER


def _setup_performance_mode(args: argparse.Namespace) -> None:
    """
    Pin the process to P-cores and raise process priority.

    Intel i7-12700K layout on Windows 11:
      Logical CPUs 0-15  = 8 P-cores x 2 HT threads (high IPC, high clock)
      Logical CPUs 16-19 = 4 E-cores x 1 thread (lower IPC, lower clock)

    Pinning to P-cores eliminates scheduling jitter from E-core context switches.
    Raising to HIGH_PRIORITY_CLASS reduces OS preemption during the polling loop.

    Requires: pip install psutil
    If psutil is unavailable or affinity fails, logs a warning and continues normally.
    """
    try:
        import psutil  # type: ignore
        import platform
        proc = psutil.Process()

        logical_cpus = psutil.cpu_count(logical=True)
        p_cores: list
        if args.p_core_affinity:
            p_cores = [int(x.strip()) for x in args.p_core_affinity.split(",") if x.strip()]
        elif logical_cpus == 20:
            p_cores = list(range(16))
            LOGGER.info("Auto-detected i7-12700K: pinning to P-cores (logical CPUs 0-15)")
        else:
            LOGGER.warning(
                "performance-mode: CPU count=%d does not match i7-12700K (20). "
                "Use --p-core-affinity to specify P-core CPU IDs explicitly. "
                "Skipping affinity.",
                logical_cpus,
            )
            p_cores = []

        if p_cores:
            proc.cpu_affinity(p_cores)
            LOGGER.info("CPU affinity set to logical CPUs: %s", p_cores)

        if platform.system() == "Windows":
            proc.nice(psutil.HIGH_PRIORITY_CLASS)
            LOGGER.info("Windows process priority set to HIGH_PRIORITY_CLASS")
        else:
            try:
                proc.nice(-10)
                LOGGER.info("Linux process nice set to -10")
            except psutil.AccessDenied:
                LOGGER.warning("Could not set nice=-10 (requires sudo). Continuing with default priority.")

    except ImportError:
        LOGGER.warning(
            "performance-mode: psutil not installed. Run 'pip install psutil' to enable "
            "CPU affinity and process priority optimizations."
        )
    except Exception as exc:
        LOGGER.warning("performance-mode setup failed (%s). Continuing normally.", exc)


def _prevent_sleep() -> None:
    """
    Tell Windows not to sleep or hibernate while this process is running.

    Uses SetThreadExecutionState — the same mechanism video players use to
    prevent sleep during playback.  ES_SYSTEM_REQUIRED keeps the machine awake;
    ES_CONTINUOUS makes the state persist until explicitly cleared.
    Screen can still turn off (ES_DISPLAY_REQUIRED is intentionally omitted).

    Automatically resets to normal power policy when the process exits via atexit.
    No-op on non-Windows platforms.
    """
    import platform
    import atexit
    import ctypes

    if platform.system() != "Windows":
        return

    try:
        ES_CONTINUOUS      = 0x80000000
        ES_SYSTEM_REQUIRED = 0x00000001
        kernel32 = ctypes.windll.kernel32

        prev = kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)
        if prev == 0:
            LOGGER.warning("SetThreadExecutionState failed — sleep prevention not active.")
            return

        atexit.register(lambda: kernel32.SetThreadExecutionState(ES_CONTINUOUS))
        LOGGER.info("Sleep prevention active (SetThreadExecutionState). Screen may still dim.")
    except Exception as exc:
        LOGGER.warning("Could not enable sleep prevention: %s", exc)
