#!/usr/bin/env python3
"""paper_engine_consumer.py -- SignalEngine fed by shared market-data IPC."""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from multiprocessing.connection import Client
from pathlib import Path
from typing import Optional, Sequence

PROJECT_DIR = Path(__file__).resolve().parents[2]
TRADING_DIR = PROJECT_DIR / "scripts" / "trading"
MONITOR_DIR = PROJECT_DIR / "scripts" / "monitor"
ANALYSIS_DIR = PROJECT_DIR / "scripts" / "analysis"
for _p in (str(TRADING_DIR), str(MONITOR_DIR), str(ANALYSIS_DIR), str(PROJECT_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from monitor_system import _prevent_sleep, _setup_performance_mode  # noqa: E402
from shared_capture import SharedCaptureClient  # noqa: E402
from shared_market_data import decode_state, decode_tick_batch, parse_utc_iso  # noqa: E402
from signal_engine import (  # noqa: E402
    SignalEngine,
    _check_gate_threshold_drift,
    _log_gate_threshold_drift,
    _log_mode_lever_summary,
    parse_trade_args,
)


LOGGER = logging.getLogger("paper_engine_consumer")


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Paper SignalEngine consumer for shared market data.")
    p.add_argument("--bus-host", default="127.0.0.1")
    p.add_argument("--bus-port", type=int, required=True)
    p.add_argument("--bus-authkey", required=True)
    p.add_argument("--watcher-pid", type=int, default=0)
    p.add_argument("--consumer-connect-timeout-secs", type=float, default=45.0)
    p.add_argument("--capture-bus-host", default="")
    p.add_argument("--capture-bus-port", type=int, default=0)
    p.add_argument("--capture-bus-authkey", default="")
    p.add_argument("--shared-capture-timeout-secs", type=float, default=8.0)
    args, remaining = p.parse_known_args(argv)
    trade_args, monitor_args = parse_trade_args(remaining)
    args.trade_args = trade_args
    args.monitor_args = monitor_args
    return args


def _connect_with_timeout(args: argparse.Namespace):
    deadline = time.time() + max(1.0, float(args.consumer_connect_timeout_secs))
    last_exc: Optional[BaseException] = None
    while time.time() < deadline:
        try:
            return Client(
                (str(args.bus_host), int(args.bus_port)),
                authkey=str(args.bus_authkey).encode("utf-8"),
            )
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            time.sleep(0.5)
    raise RuntimeError(
        f"could not connect to market-data watcher at {args.bus_host}:{args.bus_port}: {last_exc}"
    )


def _init_market_data_stats(engine: SignalEngine, args: argparse.Namespace) -> None:
    engine._market_data_mode = "shared_consumer"
    engine._market_data_health = {
        "market_data_mode": "shared_consumer",
        "consumer_pid": os.getpid(),
        "watcher_pid": int(getattr(args, "watcher_pid", 0) or 0),
        "bus_host": str(args.bus_host),
        "bus_port": int(args.bus_port),
        "batches_received": 0,
        "last_market_data_sequence": 0,
        "market_data_gap_count": 0,
        "max_market_data_lag_ms": 0.0,
        "consumer_disconnects": 0,
        "shutdown_received": False,
        "last_watcher_health": {},
        "shared_capture_enabled": False,
        "shared_capture_stats": {},
    }


def _update_market_data_stats(engine: SignalEngine, payload: dict) -> None:
    stats = getattr(engine, "_market_data_health", {})
    seq = int(payload.get("sequence") or 0)
    prior = int(stats.get("last_market_data_sequence") or 0)
    if prior and seq > prior + 1:
        stats["market_data_gap_count"] = int(stats.get("market_data_gap_count") or 0) + (seq - prior - 1)
    emitted_ts = parse_utc_iso(payload.get("emitted_at_utc"))
    if emitted_ts > 0:
        engine._shared_market_data_emitted_ts = emitted_ts
        lag_ms = max(0.0, (time.time() - emitted_ts) * 1000.0)
        stats["max_market_data_lag_ms"] = round(max(float(stats.get("max_market_data_lag_ms") or 0.0), lag_ms), 2)
    stats["batches_received"] = int(stats.get("batches_received") or 0) + 1
    stats["last_market_data_sequence"] = seq
    stats["last_watcher_health"] = dict(payload.get("health") or {})
    watcher_pid = (payload.get("health") or {}).get("watcher_pid")
    if watcher_pid:
        stats["watcher_pid"] = int(watcher_pid)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, str(args.monitor_args.log_level).upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    if not args.trade_args.cache_path.exists():
        LOGGER.error("Cache not found: %s", args.trade_args.cache_path)
        return 1
    _log_gate_threshold_drift(_check_gate_threshold_drift(args.trade_args))
    _log_mode_lever_summary(args.trade_args)

    if getattr(args.monitor_args, "performance_mode", False):
        _prevent_sleep()
        _setup_performance_mode(args.monitor_args)

    engine = SignalEngine(args=args.monitor_args, trade_args=args.trade_args)
    _init_market_data_stats(engine, args)
    if int(getattr(args, "capture_bus_port", 0) or 0) > 0 and str(getattr(args, "capture_bus_authkey", "") or ""):
        engine._shared_capture_client = SharedCaptureClient(
            host=str(args.capture_bus_host or args.bus_host),
            port=int(args.capture_bus_port),
            authkey=str(args.capture_bus_authkey),
            timeout_secs=float(args.shared_capture_timeout_secs),
        )
        engine._market_data_health["shared_capture_enabled"] = True
        LOGGER.info(
            "Connected shared capture client to %s:%s",
            str(args.capture_bus_host or args.bus_host),
            int(args.capture_bus_port),
        )
    conn = None
    try:
        conn = _connect_with_timeout(args)
        LOGGER.info(
            "Connected to shared market-data watcher at %s:%s",
            args.bus_host,
            args.bus_port,
        )
        while True:
            try:
                payload = conn.recv()
            except EOFError:
                engine._market_data_health["consumer_disconnects"] = int(
                    engine._market_data_health.get("consumer_disconnects") or 0
                ) + 1
                LOGGER.warning("Market-data watcher disconnected.")
                break
            if not isinstance(payload, dict):
                continue
            msg_type = str(payload.get("type") or "")
            if msg_type == "shutdown":
                engine._market_data_health["shutdown_received"] = True
                engine._market_data_health["last_market_data_sequence"] = int(payload.get("sequence") or 0)
                LOGGER.info("Market-data watcher shutdown received: %s", payload.get("reason"))
                break
            if msg_type != "tick_batch":
                continue
            _update_market_data_stats(engine, payload)
            games, matches, active_games = decode_state(payload)
            engine.games = games
            engine.matches = matches
            engine.active_games = active_games
            engine._on_tick_batch(decode_tick_batch(payload))
    except KeyboardInterrupt:
        LOGGER.info("Interrupted.")
        return 130
    except Exception as exc:  # noqa: BLE001
        LOGGER.exception("Paper shared consumer failed: %s", exc)
        return 1
    finally:
        try:
            if conn is not None:
                conn.close()
        except Exception:
            pass
        try:
            engine._on_tick_batch([])
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("Final empty tick hook failed: %s", exc)
        try:
            engine._log_runtime_debug_rollups(force=True)
        except Exception:
            pass
        try:
            capture_client = getattr(engine, "_shared_capture_client", None)
            if capture_client is not None:
                engine._market_data_health["shared_capture_stats"] = capture_client.stats()
        except Exception:
            pass
        LOGGER.info("Saving final shared-consumer session state.")
        # 2026-05-26 (F2 fix): force-flush the Scoped Alt-A apply-event
        # rollup before saving so the per-cohort suppressed counts hit
        # the launch_log before shutdown. Same call as live_engine's
        # graceful path.
        try:
            from signal_pipeline_gates_post_fv import flush_scoped_alt_a_rollup
            flush_scoped_alt_a_rollup(engine, force=True)
        except Exception:  # noqa: BLE001 - observability only
            pass
        try:
            engine._save_session()
        finally:
            try:
                engine._executor.shutdown(wait=False)
            except Exception:
                pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
