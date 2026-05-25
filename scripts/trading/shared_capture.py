#!/usr/bin/env python3
"""shared_capture.py -- Cached capture IPC for shared paper-engine runs.

Phase 2 of the parallel paper runner keeps raw market polling centralized and
also centralizes the expensive signal-adjacent capture calls:

  - decision-time depth snapshots (Family B)
  - recent trade tape fetches (Family A)
  - post-signal book capture time series

Consumers call the local request/response IPC client. The watcher owns the
network calls, cache, and shared capture files. Single-engine paper/live runs
never construct this client, so they keep the legacy direct-fetch behavior.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from multiprocessing.connection import Client, Listener
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

PROJECT_DIR = Path(__file__).resolve().parents[2]
MONITOR_DIR = PROJECT_DIR / "scripts" / "monitor"
for _p in (str(MONITOR_DIR), str(PROJECT_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from monitor_mlb_polymarket_ou import CLOB_BASE, _safe_float  # noqa: E402
from tape_capture import compute_family_a_features, fetch_recent_trades  # noqa: E402


LOGGER = logging.getLogger("shared_capture")

DEFAULT_DEPTH_BUCKET_MS = 1000
DEFAULT_TAPE_TTL_SECS = 5.0
DEFAULT_SIGNAL_BUCKET_MS = 1000
DEFAULT_CAPTURE_TIMEOUT_SECS = 8.0


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _bucket_ms(value_s: float, bucket_ms: int) -> int:
    bucket = max(1, int(bucket_ms))
    return int((float(value_s) * 1000.0) // bucket)


def _capture_id(*parts: object) -> str:
    text = "|".join(str(p) for p in parts)
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:20]


def fetch_depth_snapshot_with_book_client(book_client: Any, token_id: str, depth: int) -> dict:
    """Fetch one depth-aware book snapshot using the monitor's book client."""
    out: dict = {
        "ok": False,
        "latency_ms": 0.0,
        "error": "",
        "best_bid": None,
        "best_bid_size": None,
        "best_ask": None,
        "best_ask_size": None,
        "ltp": None,
        "spread": None,
        "mid": None,
        "total_bid_depth": None,
        "total_ask_depth": None,
        "top_bids": [],
        "top_asks": [],
    }
    if not token_id:
        out["error"] = "missing_token_id"
        return out

    t0 = time.time()
    try:
        sess = book_client._session()
        resp = sess.get(
            f"{CLOB_BASE}/book",
            params={"token_id": token_id},
            timeout=float(getattr(book_client, "timeout", 3.0) or 3.0),
        )
        out["latency_ms"] = round((time.time() - t0) * 1000.0, 2)
        if resp.status_code != 200:
            out["error"] = f"http_{resp.status_code}"
            return out
        data = resp.json()
        out["ltp"] = _safe_float(data.get("last_trade_price"))
        bids = sorted(
            data.get("bids", []) or [],
            key=lambda x: float(x.get("price", 0.0)),
            reverse=True,
        )
        asks = sorted(
            data.get("asks", []) or [],
            key=lambda x: float(x.get("price", 0.0)),
        )
        if bids:
            out["best_bid"] = _safe_float(bids[0].get("price"))
            out["best_bid_size"] = _safe_float(bids[0].get("size"))
            out["top_bids"] = [
                {"price": _safe_float(b.get("price")), "size": _safe_float(b.get("size"))}
                for b in bids[: max(1, int(depth))]
            ]
            out["total_bid_depth"] = round(
                sum(_safe_float(b.get("size")) or 0.0 for b in bids[: max(1, int(depth))]),
                2,
            )
        if asks:
            out["best_ask"] = _safe_float(asks[0].get("price"))
            out["best_ask_size"] = _safe_float(asks[0].get("size"))
            out["top_asks"] = [
                {"price": _safe_float(a.get("price")), "size": _safe_float(a.get("size"))}
                for a in asks[: max(1, int(depth))]
            ]
            out["total_ask_depth"] = round(
                sum(_safe_float(a.get("size")) or 0.0 for a in asks[: max(1, int(depth))]),
                2,
            )
        if out["best_bid"] is not None and out["best_ask"] is not None:
            out["spread"] = round(out["best_ask"] - out["best_bid"], 4)
            out["mid"] = round((out["best_bid"] + out["best_ask"]) / 2.0, 4)
        out["ok"] = True
    except Exception as exc:  # noqa: BLE001
        out["latency_ms"] = round((time.time() - t0) * 1000.0, 2)
        out["error"] = str(exc)
    return out


class SharedCaptureClient:
    """Small synchronous client used by paper consumers."""

    def __init__(
        self,
        *,
        host: str,
        port: int,
        authkey: str,
        timeout_secs: float = DEFAULT_CAPTURE_TIMEOUT_SECS,
    ):
        self.host = str(host)
        self.port = int(port)
        self.authkey = str(authkey).encode("utf-8")
        self.timeout_secs = max(0.5, float(timeout_secs))
        self._lock = threading.Lock()
        self._stats: Dict[str, int] = {
            "requests": 0,
            "responses_ok": 0,
            "responses_error": 0,
            "cache_hits": 0,
            "fallbacks": 0,
        }

    def stats(self) -> Dict[str, int]:
        with self._lock:
            return dict(self._stats)

    def _inc(self, key: str, amount: int = 1) -> None:
        with self._lock:
            self._stats[key] = int(self._stats.get(key, 0) or 0) + amount

    def request(self, action: str, **payload: Any) -> Optional[dict]:
        req = {
            "type": "capture_request",
            "request_id": uuid.uuid4().hex,
            "action": str(action),
            **payload,
        }
        self._inc("requests")
        conn = None
        try:
            conn = Client((self.host, self.port), authkey=self.authkey)
            conn.send(req)
            if not conn.poll(self.timeout_secs):
                raise TimeoutError(f"shared capture request timed out: {action}")
            resp = conn.recv()
            if not isinstance(resp, dict) or not resp.get("ok"):
                self._inc("responses_error")
                return resp if isinstance(resp, dict) else None
            self._inc("responses_ok")
            if bool(resp.get("cache_hit")):
                self._inc("cache_hits")
            return resp
        except Exception as exc:  # noqa: BLE001
            self._inc("responses_error")
            LOGGER.debug("Shared capture request failed action=%s: %s", action, exc)
            return None
        finally:
            try:
                if conn is not None:
                    conn.close()
            except Exception:
                pass

    def fetch_depth_snapshot(self, *, token_id: str, depth: int, bucket_ms: int = DEFAULT_DEPTH_BUCKET_MS) -> Optional[dict]:
        resp = self.request(
            "depth_snapshot",
            token_id=token_id,
            depth=int(depth),
            bucket_ms=int(bucket_ms),
        )
        if not resp:
            self._inc("fallbacks")
            return None
        return resp.get("snapshot") if isinstance(resp.get("snapshot"), dict) else None

    def capture_tape(
        self,
        *,
        token_id: str,
        signal_ts: float,
        current_ask: Optional[float],
        ttl_secs: float = DEFAULT_TAPE_TTL_SECS,
    ) -> Optional[dict]:
        resp = self.request(
            "tape_capture",
            token_id=token_id,
            signal_ts=float(signal_ts),
            current_ask=current_ask,
            ttl_secs=float(ttl_secs),
        )
        if not resp:
            self._inc("fallbacks")
            return None
        result = resp.get("result")
        return result if isinstance(result, dict) else None

    def start_book_capture(
        self,
        *,
        token_id: str,
        date_str: str,
        signal_ts: float,
        duration: float,
        interval: float,
        depth: int,
        initial_book: dict,
        header: dict,
        signal_bucket_ms: int = DEFAULT_SIGNAL_BUCKET_MS,
    ) -> Optional[dict]:
        resp = self.request(
            "start_book_capture",
            token_id=token_id,
            date_str=date_str,
            signal_ts=float(signal_ts),
            duration=float(duration),
            interval=float(interval),
            depth=int(depth),
            signal_bucket_ms=int(signal_bucket_ms),
            initial_book=dict(initial_book or {}),
            header=dict(header or {}),
        )
        if not resp:
            self._inc("fallbacks")
            return None
        return resp


@dataclass
class _CacheEntry:
    value: dict
    expires_at: float


class SharedCaptureServer:
    """Watcher-side local request server and cache owner."""

    def __init__(
        self,
        *,
        host: str,
        port: int,
        authkey: str,
        book_client: Any,
        output_root: Path,
        date_str: str,
        max_queue_requests: int = 512,
        depth_bucket_ms: int = DEFAULT_DEPTH_BUCKET_MS,
        tape_ttl_secs: float = DEFAULT_TAPE_TTL_SECS,
        signal_bucket_ms: int = DEFAULT_SIGNAL_BUCKET_MS,
    ):
        self.host = str(host)
        self.port = int(port)
        self.authkey = str(authkey).encode("utf-8")
        self.book_client = book_client
        self.output_root = Path(output_root)
        self.date_str = str(date_str)
        self.max_queue_requests = max(1, int(max_queue_requests))
        self.depth_bucket_ms = max(1, int(depth_bucket_ms))
        self.tape_ttl_secs = max(0.1, float(tape_ttl_secs))
        self.signal_bucket_ms = max(1, int(signal_bucket_ms))
        self.listener = Listener((self.host, self.port), authkey=self.authkey)
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._depth_cache: Dict[Tuple[str, int, int], dict] = {}
        self._tape_cache: Dict[str, _CacheEntry] = {}
        self._book_capture_keys: Dict[Tuple[str, int, int, int, int], dict] = {}
        self._stats: Dict[str, int] = {
            "requests": 0,
            "depth_requests": 0,
            "depth_cache_hits": 0,
            "tape_requests": 0,
            "tape_cache_hits": 0,
            "book_capture_requests": 0,
            "book_capture_cache_hits": 0,
            "errors": 0,
        }
        self._thread = threading.Thread(target=self._accept_loop, daemon=True, name="shared_capture_accept")

    def start(self) -> None:
        self._thread.start()

    def stats(self) -> Dict[str, int]:
        with self._lock:
            return dict(self._stats)

    def _inc(self, key: str, amount: int = 1) -> None:
        self._stats[key] = int(self._stats.get(key, 0) or 0) + amount

    def _accept_loop(self) -> None:
        while not self._stop.is_set():
            try:
                conn = self.listener.accept()
            except (OSError, EOFError):
                break
            except Exception as exc:  # noqa: BLE001
                LOGGER.warning("Shared capture accept failed: %s", exc)
                continue
            threading.Thread(target=self._handle_conn, args=(conn,), daemon=True, name="shared_capture_request").start()

    def _handle_conn(self, conn: Any) -> None:
        try:
            req = conn.recv()
            resp = self.handle_request(req if isinstance(req, dict) else {})
            conn.send(resp)
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("Shared capture request failed: %s", exc)
            try:
                conn.send({"ok": False, "error": str(exc)})
            except Exception:
                pass
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def handle_request(self, req: Dict[str, Any]) -> dict:
        action = str(req.get("action") or "")
        with self._lock:
            self._inc("requests")
        if action == "depth_snapshot":
            return self._handle_depth(req)
        if action == "tape_capture":
            return self._handle_tape(req)
        if action == "start_book_capture":
            return self._handle_book_capture(req)
        with self._lock:
            self._inc("errors")
        return {"ok": False, "error": f"unknown_action:{action}"}

    def _handle_depth(self, req: Dict[str, Any]) -> dict:
        token_id = str(req.get("token_id") or "")
        depth = max(1, int(req.get("depth") or 1))
        bucket_ms = max(1, int(req.get("bucket_ms") or self.depth_bucket_ms))
        bucket = _bucket_ms(time.time(), bucket_ms)
        key = (token_id, depth, bucket)
        with self._lock:
            self._inc("depth_requests")
            cached = self._depth_cache.get(key)
            if cached is not None:
                self._inc("depth_cache_hits")
                snapshot = dict(cached)
                snapshot["shared_capture_cache_hit"] = True
                return {"ok": True, "cache_hit": True, "snapshot": snapshot}
        snapshot = fetch_depth_snapshot_with_book_client(self.book_client, token_id, depth)
        snapshot["shared_capture_cache_hit"] = False
        snapshot["shared_capture_bucket_ms"] = bucket_ms
        with self._lock:
            self._depth_cache[key] = dict(snapshot)
        return {"ok": True, "cache_hit": False, "snapshot": snapshot}

    def _handle_tape(self, req: Dict[str, Any]) -> dict:
        token_id = str(req.get("token_id") or "")
        signal_ts = float(req.get("signal_ts") or time.time())
        current_ask = req.get("current_ask")
        ttl_secs = max(0.1, float(req.get("ttl_secs") or self.tape_ttl_secs))
        now = time.time()
        with self._lock:
            self._inc("tape_requests")
            entry = self._tape_cache.get(token_id)
            if entry is not None and entry.expires_at >= now:
                self._inc("tape_cache_hits")
                fetch = dict(entry.value)
                cache_hit = True
            else:
                fetch = {}
                cache_hit = False
        if not cache_hit:
            fetch = fetch_recent_trades(
                token_id=token_id,
                session=self.book_client._session(),
            )
            with self._lock:
                self._tape_cache[token_id] = _CacheEntry(value=dict(fetch), expires_at=now + ttl_secs)
        features = compute_family_a_features(
            trades=list(fetch.get("trades") or []),
            signal_ts=signal_ts,
            current_ask=_safe_float(current_ask),
        )
        features["fetch_ok"] = fetch.get("ok", False)
        features["fetch_error"] = fetch.get("error")
        features["fetch_latency_ms"] = fetch.get("latency_ms", 0.0)
        features["shared_capture_cache_hit"] = cache_hit
        result = {
            "token_id": token_id,
            "signal_ts": signal_ts,
            "current_ask": current_ask,
            "trades": fetch.get("trades", []),
            "features": features,
            "shared_capture_cache_hit": cache_hit,
        }
        return {"ok": True, "cache_hit": cache_hit, "result": result}

    def _book_capture_path(self, capture_id: str, date_str: str) -> Path:
        root = self.output_root / "shared_book_captures" / date_str
        root.mkdir(parents=True, exist_ok=True)
        return root / f"{capture_id}.jsonl"

    def _handle_book_capture(self, req: Dict[str, Any]) -> dict:
        token_id = str(req.get("token_id") or "")
        date_str = str(req.get("date_str") or self.date_str)
        signal_ts = float(req.get("signal_ts") or time.time())
        duration = max(0.0, float(req.get("duration") or 0.0))
        interval = max(0.1, float(req.get("interval") or 1.0))
        depth = max(1, int(req.get("depth") or 1))
        bucket_ms = max(1, int(req.get("signal_bucket_ms") or self.signal_bucket_ms))
        signal_bucket = _bucket_ms(signal_ts, bucket_ms)
        duration_ms = int(round(duration * 1000.0))
        interval_ms = int(round(interval * 1000.0))
        key = (token_id, signal_bucket, duration_ms, interval_ms, depth)
        with self._lock:
            self._inc("book_capture_requests")
            existing = self._book_capture_keys.get(key)
            if existing is not None:
                self._inc("book_capture_cache_hits")
                return {"ok": True, "cache_hit": True, **existing}

            capture_id = _capture_id(token_id, signal_bucket, duration_ms, interval_ms, depth)
            path = self._book_capture_path(capture_id, date_str)
            meta = {
                "shared_capture_id": capture_id,
                "shared_capture_path": str(path),
                "shared_capture_key": {
                    "token_id": token_id,
                    "signal_bucket": signal_bucket,
                    "duration_ms": duration_ms,
                    "interval_ms": interval_ms,
                    "depth": depth,
                },
            }
            self._book_capture_keys[key] = dict(meta)

        header = dict(req.get("header") or {})
        header.update(
            {
                "type": "signal",
                "bet_id": f"shared_{capture_id}",
                "token_id": token_id,
                "shared_capture_id": capture_id,
                "shared_capture_source": "watcher",
                "capture_duration_s": duration,
                "capture_interval_s": interval,
                "capture_depth": depth,
            }
        )
        initial_book = dict(req.get("initial_book") or {})
        threading.Thread(
            target=self._run_book_capture,
            args=(path, header, initial_book, signal_ts, duration, interval, depth, token_id),
            daemon=True,
            name=f"shared_book_capture_{capture_id}",
        ).start()
        return {"ok": True, "cache_hit": False, **meta}

    def _run_book_capture(
        self,
        path: Path,
        header: dict,
        initial_book: dict,
        signal_ts: float,
        duration: float,
        interval: float,
        depth: int,
        token_id: str,
    ) -> None:
        try:
            with path.open("w", encoding="utf-8") as fh:
                fh.write(json.dumps(header) + "\n")
                fh.write(
                    json.dumps(
                        {
                            "type": "snapshot",
                            "seq": 0,
                            "elapsed_s": 0.0,
                            "ts": header.get("ts") or _now_iso(),
                            "book": initial_book,
                        }
                    )
                    + "\n"
                )
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("Shared book capture header write failed path=%s: %s", path, exc)
            return

        n_snapshots = int(duration / interval) if duration > 0 else 0
        for seq in range(1, n_snapshots + 1):
            target_t = signal_ts + seq * interval
            sleep_s = max(0.0, target_t - time.time())
            if sleep_s > 0:
                time.sleep(sleep_s)
            book = fetch_depth_snapshot_with_book_client(self.book_client, token_id, depth)
            snapshot = {
                "type": "snapshot",
                "seq": seq,
                "elapsed_s": round(time.time() - signal_ts, 3),
                "ts": _now_iso(),
                "book": book,
            }
            try:
                with path.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(snapshot) + "\n")
            except Exception as exc:  # noqa: BLE001
                LOGGER.warning("Shared book capture write failed path=%s seq=%d: %s", path, seq, exc)
                break

    def close(self) -> None:
        self._stop.set()
        try:
            self.listener.close()
        except Exception:
            pass
        try:
            self._thread.join(timeout=2.0)
        except Exception:
            pass
