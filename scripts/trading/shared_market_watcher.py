#!/usr/bin/env python3
"""shared_market_watcher.py -- One market-data producer for many paper engines."""

from __future__ import annotations

import argparse
import json
import logging
import os
import queue
import signal
import sys
import threading
import time
from dataclasses import dataclass, field
from multiprocessing.connection import Listener
from pathlib import Path
from typing import Dict, List, Optional, Sequence

PROJECT_DIR = Path(__file__).resolve().parents[2]
TRADING_DIR = PROJECT_DIR / "scripts" / "trading"
MONITOR_DIR = PROJECT_DIR / "scripts" / "monitor"
for _p in (str(TRADING_DIR), str(MONITOR_DIR), str(PROJECT_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from monitor_cli import parse_args as monitor_parse_args  # noqa: E402
from monitor_mlb_polymarket_ou import MLBPolymarketMonitor  # noqa: E402
from shared_capture import SharedCaptureServer  # noqa: E402
from shared_market_data import encode_batch, shutdown_payload  # noqa: E402


LOGGER = logging.getLogger("shared_market_watcher")


@dataclass
class ClientState:
    name: str
    conn: object
    queue: "queue.Queue[Optional[dict]]"
    thread: threading.Thread
    connected_at_sequence: int
    sent: int = 0
    dropped: int = 0
    send_errors: int = 0
    closed: bool = False


class MarketDataBroadcaster:
    def __init__(
        self,
        *,
        host: str,
        port: int,
        authkey: str,
        max_queue_batches: int,
        warn_queue_batches: int,
    ):
        self.host = host
        self.port = int(port)
        self.authkey = authkey.encode("utf-8")
        self.max_queue_batches = max(1, int(max_queue_batches))
        self.warn_queue_batches = max(1, int(warn_queue_batches))
        self.listener = Listener((self.host, self.port), authkey=self.authkey)
        self.address = self.listener.address
        self._lock = threading.Lock()
        self._clients: Dict[str, ClientState] = {}
        self._last_payload: Optional[dict] = None
        self._next_client_id = 1
        self._accept_stop = threading.Event()
        self._accept_thread = threading.Thread(
            target=self._accept_loop,
            daemon=True,
            name="shared_market_accept",
        )
        self._accept_thread.start()

    def _accept_loop(self) -> None:
        while not self._accept_stop.is_set():
            try:
                conn = self.listener.accept()
            except (OSError, EOFError):
                break
            except Exception as exc:  # noqa: BLE001
                LOGGER.warning("Market-data bus accept failed: %s", exc)
                continue
            with self._lock:
                name = f"consumer_{self._next_client_id}"
                self._next_client_id += 1
                q: "queue.Queue[Optional[dict]]" = queue.Queue(maxsize=self.max_queue_batches)
                state = ClientState(
                    name=name,
                    conn=conn,
                    queue=q,
                    thread=threading.Thread(target=self._send_loop, args=(name,), daemon=True, name=f"shared_market_send_{name}"),
                    connected_at_sequence=int((self._last_payload or {}).get("sequence") or 0),
                )
                self._clients[name] = state
                state.thread.start()
                if self._last_payload is not None:
                    self._enqueue_locked(state, self._last_payload)
            LOGGER.info("Market-data consumer connected: %s", name)

    def _send_loop(self, name: str) -> None:
        while True:
            with self._lock:
                state = self._clients.get(name)
            if state is None:
                return
            item = state.queue.get()
            if item is None:
                return
            try:
                state.conn.send(item)
                state.sent += 1
            except Exception as exc:  # noqa: BLE001
                state.send_errors += 1
                state.closed = True
                LOGGER.warning("Market-data send failed for %s: %s", name, exc)
                with self._lock:
                    self._clients.pop(name, None)
                try:
                    state.conn.close()
                except Exception:
                    pass
                return

    def _enqueue_locked(self, state: ClientState, payload: dict) -> None:
        try:
            state.queue.put_nowait(payload)
        except queue.Full:
            state.dropped += 1
            try:
                state.queue.get_nowait()
            except queue.Empty:
                pass
            try:
                state.queue.put_nowait(payload)
            except queue.Full:
                state.dropped += 1
        if state.queue.qsize() >= self.warn_queue_batches:
            LOGGER.warning(
                "Market-data consumer lagging: %s queue=%d dropped=%d sent=%d",
                state.name,
                state.queue.qsize(),
                state.dropped,
                state.sent,
            )

    def broadcast(self, payload: dict) -> None:
        with self._lock:
            self._last_payload = payload
            clients = list(self._clients.values())
            for state in clients:
                if not state.closed:
                    self._enqueue_locked(state, payload)

    def health(self) -> dict:
        with self._lock:
            clients = list(self._clients.values())
        return {
            "bus_clients": len(clients),
            "bus_client_sent": {c.name: c.sent for c in clients},
            "bus_client_dropped": {c.name: c.dropped for c in clients},
            "bus_client_queue": {c.name: c.queue.qsize() for c in clients},
            "bus_client_send_errors": {c.name: c.send_errors for c in clients},
        }

    def client_count(self) -> int:
        with self._lock:
            return len(self._clients)

    def wait_for_clients(self, expected_clients: int, timeout_secs: float) -> bool:
        if expected_clients <= 0:
            return True
        deadline = time.time() + max(0.0, float(timeout_secs))
        while time.time() <= deadline:
            if self.client_count() >= expected_clients:
                return True
            time.sleep(0.05)
        return self.client_count() >= expected_clients

    def close(self, *, final_payload: Optional[dict] = None) -> None:
        self._accept_stop.set()
        try:
            self.listener.close()
        except Exception:
            pass
        if final_payload is not None:
            self.broadcast(final_payload)
        with self._lock:
            clients = list(self._clients.values())
        for state in clients:
            try:
                state.queue.put(None, timeout=1.0)
            except Exception:
                pass
        for state in clients:
            try:
                state.thread.join(timeout=2.0)
            except Exception:
                pass
        with self._lock:
            self._clients.clear()
        for state in clients:
            try:
                state.conn.close()
            except Exception:
                pass


class SharedMarketDataWatcher(MLBPolymarketMonitor):
    def __init__(self, args: argparse.Namespace, broadcaster: MarketDataBroadcaster):
        super().__init__(args)
        self.broadcaster = broadcaster
        self._shared_sequence = 0
        self._last_book_jobs = 0

    def _build_poll_jobs(self):  # type: ignore[no-untyped-def]
        jobs = super()._build_poll_jobs()
        self._last_book_jobs = len(jobs)
        return jobs

    def _on_tick_batch(self, tick_batch: list) -> None:
        self._shared_sequence += 1
        health = {
            "watcher_pid": os.getpid(),
            "poll_cycles": int(getattr(self, "_poll_cycles", 0) or 0),
            "tick_snapshots_written": int(getattr(self, "_tick_snapshots_written", 0) or 0),
            "book_jobs": int(getattr(self, "_last_book_jobs", 0) or 0),
            "active_games": sum(1 for v in getattr(self, "active_games", {}).values() if v),
            "schedule_refreshes": int(getattr(self, "_schedule_refresh_count", 0) or 0),
            "schedule_refresh_errors": int(getattr(self, "_schedule_refresh_error_count", 0) or 0),
            "retired_book_keys": len(getattr(self, "_book_fail_retire_rollup", {}) or {}),
        }
        capture_server = getattr(self, "capture_server", None)
        if capture_server is not None:
            try:
                health["shared_capture"] = capture_server.stats()
            except Exception:
                pass
        health.update(self.broadcaster.health())
        self.broadcaster.broadcast(
            encode_batch(
                sequence=self._shared_sequence,
                date_str=self.date_str,
                games=self.games,
                matches=self.matches,
                active_games=self.active_games,
                tick_batch=tick_batch,
                health=health,
            )
        )


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Shared market-data watcher for parallel paper engines.")
    p.add_argument("--bus-host", default="127.0.0.1")
    p.add_argument("--bus-port", type=int, required=True)
    p.add_argument("--bus-authkey", required=True)
    p.add_argument("--ready-file", type=Path, default=None)
    p.add_argument("--bus-max-queue-batches", type=int, default=120)
    p.add_argument("--bus-warn-queue-batches", type=int, default=50)
    p.add_argument("--expected-consumers", type=int, default=0)
    p.add_argument("--consumer-wait-timeout-secs", type=float, default=45.0)
    p.add_argument("--capture-bus-host", default="127.0.0.1")
    p.add_argument("--capture-bus-port", type=int, default=0)
    p.add_argument("--capture-bus-authkey", default="")
    p.add_argument("--shared-capture-root", type=Path, default=None)
    p.add_argument("--shared-capture-depth-bucket-ms", type=int, default=1000)
    p.add_argument("--shared-capture-tape-ttl-secs", type=float, default=5.0)
    p.add_argument("--shared-capture-signal-bucket-ms", type=int, default=1000)
    args, remaining = p.parse_known_args(argv)
    old_argv = sys.argv
    sys.argv = [old_argv[0]] + list(remaining)
    try:
        args.monitor_args = monitor_parse_args()
    finally:
        sys.argv = old_argv
    return args


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, str(args.monitor_args.log_level).upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    broadcaster = MarketDataBroadcaster(
        host=args.bus_host,
        port=args.bus_port,
        authkey=args.bus_authkey,
        max_queue_batches=args.bus_max_queue_batches,
        warn_queue_batches=args.bus_warn_queue_batches,
    )
    watcher = SharedMarketDataWatcher(args=args.monitor_args, broadcaster=broadcaster)
    capture_server: Optional[SharedCaptureServer] = None
    if args.capture_bus_port > 0 and args.capture_bus_authkey:
        capture_root = args.shared_capture_root or args.monitor_args.output_root
        capture_server = SharedCaptureServer(
            host=args.capture_bus_host,
            port=args.capture_bus_port,
            authkey=args.capture_bus_authkey,
            book_client=watcher.book_client,
            output_root=capture_root,
            date_str=watcher.date_str,
            depth_bucket_ms=args.shared_capture_depth_bucket_ms,
            tape_ttl_secs=args.shared_capture_tape_ttl_secs,
            signal_bucket_ms=args.shared_capture_signal_bucket_ms,
        )
        watcher.capture_server = capture_server
        capture_server.start()
        LOGGER.info(
            "Shared capture server ready at %s:%s root=%s",
            args.capture_bus_host,
            args.capture_bus_port,
            capture_root,
        )
    if args.ready_file:
        args.ready_file.parent.mkdir(parents=True, exist_ok=True)
        args.ready_file.write_text(
            json.dumps(
                {
                    "ready": True,
                    "pid": os.getpid(),
                    "bus_host": args.bus_host,
                    "bus_port": args.bus_port,
                    "date_str": watcher.date_str,
                },
                indent=2,
            ),
            encoding="utf-8",
        )
    if args.expected_consumers > 0:
        LOGGER.info(
            "Waiting for %d market-data consumer(s) before polling (timeout %.1fs).",
            args.expected_consumers,
            args.consumer_wait_timeout_secs,
        )
        if broadcaster.wait_for_clients(args.expected_consumers, args.consumer_wait_timeout_secs):
            LOGGER.info("All expected market-data consumers connected.")
        else:
            LOGGER.warning(
                "Starting watcher with %d/%d expected market-data consumer(s) connected.",
                broadcaster.client_count(),
                args.expected_consumers,
            )
    stop_requested = False

    def _signal_handler(signum, _frame) -> None:  # type: ignore[no-untyped-def]
        nonlocal stop_requested
        stop_requested = True
        LOGGER.info("Shared watcher received signal %s; stopping.", signum)
        watcher.stop_wall = 0

    old_int = signal.signal(signal.SIGINT, _signal_handler)
    old_term = signal.signal(signal.SIGTERM, _signal_handler)
    try:
        watcher.run()
        return 0
    except KeyboardInterrupt:
        LOGGER.info("Shared watcher interrupted.")
        return 130
    finally:
        signal.signal(signal.SIGINT, old_int)
        signal.signal(signal.SIGTERM, old_term)
        broadcaster.close(
            final_payload=shutdown_payload(
                sequence=int(getattr(watcher, "_shared_sequence", 0) or 0) + 1,
                date_str=getattr(watcher, "date_str", ""),
                reason="signal" if stop_requested else "watcher_exit",
            )
        )
        if capture_server is not None:
            LOGGER.info("Shared capture rollup: %s", capture_server.stats())
            capture_server.close()


if __name__ == "__main__":
    raise SystemExit(main())
