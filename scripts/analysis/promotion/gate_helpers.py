"""Gate -> CLI-flag mapping + value-type coercion.

Centralised so a future agent maintains the binding in one place.
"""
from __future__ import annotations

from typing import Any, Dict, Optional


# Centralised gate -> CLI flag mapping. Keep in sync with
# `live_engine_cli.py`'s argparse definitions.
_GATE_CLI_FLAGS: Dict[str, str] = {
    "gate_extreme_edge": "--extreme-edge-max",
    "gate_min_edge": "--edge-threshold",
    "gate_min_inning": "--min-inning",
    "gate_min_entry_ask": "--min-entry-ask",
    "gate_runs_needed_max": "--runs-needed-max",
}

# Per-gate value type. Used when writing to the runtime overrides file so
# `live_engine_cli.py`'s argparse-typed targets receive the right type
# (extreme_edge_max is float; min_inning is int). Keep in sync with
# signal_config.py argparse definitions.
_GATE_VALUE_TYPE: Dict[str, type] = {
    "gate_extreme_edge": float,
    "gate_min_edge": float,
    "gate_min_inning": int,
    "gate_min_entry_ask": float,
    "gate_runs_needed_max": float,
}


def _gate_cli_flag(gate_name: str) -> Optional[str]:
    return _GATE_CLI_FLAGS.get(gate_name)


def _parse_gate_value(gate_name: str, raw: Any) -> Any:
    """Coerce a raw gate-threshold value (typically a CLI-supplied string)
    to the type the live engine expects. Returns the raw value unchanged
    when the gate isn't in `_GATE_VALUE_TYPE` (defensive: the caller
    has already validated the gate name via `_gate_cli_flag`)."""
    target = _GATE_VALUE_TYPE.get(gate_name)
    if target is None or isinstance(raw, target):
        return raw
    return target(raw)
