"""Polymarket CLOB book fetch with Gamma API fallback for not-yet-deployed markets.

Thread-local ``requests.Session`` so the persistent thread pool in the monitor
keeps connections warm across poll cycles.
"""

from __future__ import annotations

import json
import threading
import time

import requests

from monitor_constants import CLOB_BASE, GAMMA_BASE
from monitor_utils import _safe_float


class PolymarketBookClient:
    def __init__(self, timeout: float = 3.0):
        self.timeout = timeout
        self.local = threading.local()

    def _session(self) -> requests.Session:
        sess = getattr(self.local, "session", None)
        if sess is None:
            sess = requests.Session()
            sess.headers.update({"User-Agent": "MLB-Poly-OU-Monitor/1.0", "Accept": "application/json"})
            adapter = requests.adapters.HTTPAdapter(pool_connections=32, pool_maxsize=32, max_retries=0)
            sess.mount("https://", adapter)
            sess.mount("http://", adapter)
            self.local.session = sess
        return sess

    def fetch_book(self, token_id: str, market_id: str = "", token_index: int = 0) -> dict:
        out = {
            "ok": False,
            "status_code": None,
            "error": "",
            "latency_ms": 0.0,
            "hash": "",
            "api_ts": "",
            "best_bid": None,
            "best_bid_size": None,
            "best_ask": None,
            "best_ask_size": None,
            "ltp": None,
            "source": "clob",
        }
        t0 = time.time()
        try:
            url = f"{CLOB_BASE}/book"
            resp = self._session().get(url, params={"token_id": token_id}, timeout=self.timeout)
            out["status_code"] = resp.status_code
            out["latency_ms"] = round((time.time() - t0) * 1000.0, 2)
            if resp.status_code != 200:
                out["error"] = f"http_{resp.status_code}"
                if resp.status_code == 404 and market_id:
                    # CLOB orderbook not deployed yet — fall back to Gamma API prices.
                    gamma_result = self.fetch_book_from_gamma(market_id, token_index)
                    return gamma_result
                return out
            payload = resp.json()
            out["hash"] = str(payload.get("hash") or "")
            out["api_ts"] = str(payload.get("timestamp") or "")
            out["ltp"] = _safe_float(payload.get("last_trade_price"))

            bids = payload.get("bids", []) or []
            asks = payload.get("asks", []) or []
            if bids:
                best_bid = max(bids, key=lambda x: float(x.get("price", 0.0)))
                out["best_bid"] = _safe_float(best_bid.get("price"))
                out["best_bid_size"] = _safe_float(best_bid.get("size"))
            if asks:
                best_ask = min(asks, key=lambda x: float(x.get("price", 0.0)))
                out["best_ask"] = _safe_float(best_ask.get("price"))
                out["best_ask_size"] = _safe_float(best_ask.get("size"))

            out["ok"] = True
            return out
        except Exception as exc:
            out["latency_ms"] = round((time.time() - t0) * 1000.0, 2)
            out["error"] = str(exc)
            return out

    def fetch_book_from_gamma(self, market_id: str, token_index: int) -> dict:
        """Fallback price source when CLOB book is unavailable (market not yet deployed).

        Fetches outcomePrices from Gamma API. token_index 0 = over/yes, 1 = under/no.
        Bid/ask are estimated with a narrow spread around the outcome mid-price.
        """
        out = {
            "ok": False,
            "status_code": None,
            "error": "",
            "latency_ms": 0.0,
            "hash": "",
            "api_ts": "",
            "best_bid": None,
            "best_bid_size": None,
            "best_ask": None,
            "best_ask_size": None,
            "ltp": None,
            "source": "gamma",
        }
        t0 = time.time()
        try:
            url = f"{GAMMA_BASE}/markets/{market_id}"
            resp = self._session().get(url, timeout=self.timeout)
            out["status_code"] = resp.status_code
            out["latency_ms"] = round((time.time() - t0) * 1000.0, 2)
            if resp.status_code != 200:
                out["error"] = f"gamma_http_{resp.status_code}"
                return out
            mkt = resp.json()
            # outcomePrices is a JSON-encoded string: '["0.72", "0.28"]'
            prices_raw = mkt.get("outcomePrices", "[]")
            try:
                prices = json.loads(prices_raw) if isinstance(prices_raw, str) else list(prices_raw)
            except Exception:
                out["error"] = "gamma_prices_parse_error"
                return out
            if not isinstance(prices, list) or token_index >= len(prices):
                out["error"] = f"gamma_no_price_at_index_{token_index}"
                return out
            mid = _safe_float(prices[token_index])
            if mid is None or mid <= 0 or mid >= 1:
                out["error"] = f"gamma_invalid_price:{prices[token_index]}"
                return out
            half = 0.01
            out["best_bid"] = round(max(0.01, mid - half), 2)
            out["best_ask"] = round(min(0.99, mid + half), 2)
            out["api_ts"] = str(mkt.get("updatedAt") or "")
            out["ok"] = True
            return out
        except Exception as exc:
            out["latency_ms"] = round((time.time() - t0) * 1000.0, 2)
            out["error"] = str(exc)
            return out
