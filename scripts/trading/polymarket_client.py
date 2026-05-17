"""
polymarket_client.py -- Authenticated wrapper around py-clob-client-v2 for Polymarket CLOB.

Handles:
  - Credential loading from environment (.env)
  - One-time API key derivation at startup
  - Limit BUY order placement (signed + posted)
  - Order status polling
  - Order cancellation
  - Transient-error retry with exponential backoff

This module has NO trading logic. It only knows about the CLOB API.
Imported by live_engine.py; can be tested independently with test_order.py.

Requires:
    pip install py-clob-client-v2 python-dotenv eth-account

CLOB V2 migration notes (2026-04-27 -> cutover 2026-04-28 ~11:00 UTC):
  - SDK swapped to py-clob-client-v2 (V1 stops working post-cutover)
  - OrderArgs -> OrderArgsV2 (same 4 required fields; aliased here)
  - cancel(order_id) -> cancel_order(OrderPayload(orderID=order_id))
  - Constructor kwargs unchanged (chain_id, signature_type, funder)
  - Pre-cutover testing URL is https://clob-v2.polymarket.com; set
    POLY_CLOB_HOST env var to override default before cutover.
  - Post-cutover (Apr 28 +11:00 UTC) the production URL serves V2.
  - See model_improvements/clob_v2_cp1_findings.txt for full inspection.

Deposit Wallet rollout resilience (2026-05-04):
  - All API calls now retry on transient HTTP errors (5xx, timeouts,
    connection refused) with exponential backoff (2s/4s/8s, 3 retries).
  - 4xx client errors (bad signature, insufficient balance) are NOT retried.
  - health_check() probes CLOB availability; used by --wait-for-clob.

Deposit Wallet (ERC-1271 / sig_type=3) opt-in path (2026-05-04 cutover):
  - Polymarket added a new wallet path that signs orders via ERC-1271
    on a deterministic deposit-wallet smart account, addressing the
    "ghost fills" issue. Existing proxy/Safe users (sig_type=2) are
    unaffected; the new path is OPT-IN.
  - Activate by either:
      * setting POLY_USE_DEPOSIT_WALLET=1 + POLY_DEPOSIT_WALLET=0x... in .env, OR
      * passing use_deposit_wallet=True + deposit_wallet="0x..." to the constructor.
  - Requires py-clob-client-v2 >= 1.0.1rc1 (which exposes signature_type=3).
  - The deposit wallet must already be deployed and funded with USDC on
    Polygon mainnet via the Polymarket factory at
    0x00000000000Fb5C9ADea0298D729A0CB3823Cc07. pUSD held by the EOA does
    NOT count as CLOB buying power once the deposit-wallet path is active.
  - When sig_type=3 is active, both `funder` and `maker_address` resolve
    to the deposit wallet (not the EOA, not the legacy proxy). API key
    derivation hits the deposit-wallet ERC-1271 endpoint; existing EOA-
    derived API creds are NOT reused.
  - See model_improvements/deposit_wallet_migration_plan.txt for the
    full one-time funding + cutover runbook.
"""

from __future__ import annotations

import logging
import math
import os
import time
from pathlib import Path
from typing import Optional

from models import OrderResult, OrderStatus, TradeRecord, _ts_to_iso  # noqa: E402

LOGGER = logging.getLogger("polymarket_client")

# Allow per-environment override (e.g. set POLY_CLOB_HOST=https://clob-v2.polymarket.com
# during pre-cutover testing). Default is production URL, which serves V2 post-cutover.
CLOB_HOST = os.environ.get("POLY_CLOB_HOST") or "https://clob.polymarket.com"
CHAIN_ID  = 137  # Polygon mainnet

# Public Polymarket data-api: positions + trades indexed by wallet address.
# Used by the orphan-fill reconciler to catch fills the SDK's maker-address
# filtered get_trades misses (taker-side fills, post-resolution lookups, etc.).
DATA_API_BASE = os.environ.get("POLY_DATA_API_HOST") or "https://data-api.polymarket.com"
DATA_API_DEFAULT_TIMEOUT_SECS = 8.0

# ERC-1271 deposit-wallet signature type (Polymarket SDK constant for POLY_1271).
# sig_type 0 = EOA, 1 = legacy proxy, 2 = Safe/proxy (current default), 3 = ERC-1271 deposit wallet.
SIG_TYPE_DEPOSIT_WALLET = 3

# Retry configuration for transient CLOB errors
RETRY_MAX_ATTEMPTS = 3
RETRY_BASE_DELAY_SECS = 2.0  # exponential: 2s, 4s, 8s


# ---------------------------------------------------------------------------
# Transient error detection
# ---------------------------------------------------------------------------

def _is_transient_error(exc: Exception) -> bool:
    """Return True if the exception looks like a transient CLOB/network error.

    Retryable: HTTP 5xx, connection refused/reset, timeouts.
    NOT retryable: HTTP 4xx (bad request, auth failure, insufficient balance).
    """
    exc_type = type(exc).__name__
    exc_module = type(exc).__module__ or ""

    # httpx-specific errors (used by py-clob-client-v2)
    try:
        import httpx
        if isinstance(exc, httpx.TimeoutException):
            return True
        if isinstance(exc, httpx.ConnectError):
            return True
        if isinstance(exc, httpx.HTTPStatusError):
            return exc.response.status_code >= 500
    except ImportError:
        pass

    # requests-specific errors (fallback if any code path uses requests)
    try:
        import requests
        if isinstance(exc, (requests.ConnectionError, requests.Timeout)):
            return True
        if isinstance(exc, requests.HTTPError):
            resp = getattr(exc, "response", None)
            if resp is not None and resp.status_code >= 500:
                return True
    except ImportError:
        pass

    # Generic network/OS errors
    if isinstance(exc, (ConnectionError, ConnectionResetError, ConnectionRefusedError, TimeoutError, OSError)):
        return True

    # Check error message for common transient patterns
    msg = str(exc).lower()
    transient_patterns = [
        "502", "503", "504", "520", "521", "522", "524",
        "bad gateway", "service unavailable", "gateway timeout",
        "connection refused", "connection reset",
        "timed out", "timeout", "temporarily unavailable",
        "internal server error",
    ]
    return any(p in msg for p in transient_patterns)


def _retry_on_transient(
    func,
    *args,
    max_attempts: int = RETRY_MAX_ATTEMPTS,
    base_delay: float = RETRY_BASE_DELAY_SECS,
    operation_name: str = "CLOB call",
    **kwargs,
):
    """Execute *func* with retry on transient errors.

    Raises the last exception if all attempts fail.
    """
    last_exc = None
    for attempt in range(1, max_attempts + 1):
        try:
            return func(*args, **kwargs)
        except Exception as exc:
            if not _is_transient_error(exc) or attempt == max_attempts:
                if attempt > 1:
                    LOGGER.warning(
                        "%s failed after %d attempt(s): %s",
                        operation_name, attempt, exc,
                    )
                raise
            last_exc = exc
            delay = base_delay * (2 ** (attempt - 1))
            LOGGER.warning(
                "%s transient error (attempt %d/%d), retrying in %.1fs: %s",
                operation_name, attempt, max_attempts, delay, exc,
            )
            time.sleep(delay)
    raise last_exc  # unreachable but keeps linters happy


# ---------------------------------------------------------------------------
# CLOBOrderClient
# ---------------------------------------------------------------------------

class CLOBOrderClient:
    """
    Thin authenticated wrapper around py-clob-client.

    Usage:
        client = CLOBOrderClient.from_env()
        client.initialize()          # derives API keys — call once at startup
        result = client.place_limit_buy(token_id, price=0.72, size_usdc=100.0)
        if result.success:
            status = client.get_order(result.order_id)
    """

    def __init__(
        self,
        private_key: str,
        funder: Optional[str] = None,
        host: str = CLOB_HOST,
        chain_id: int = CHAIN_ID,
        dry_run: bool = False,
        *,
        use_deposit_wallet: bool = False,
        deposit_wallet: Optional[str] = None,
    ) -> None:
        if not private_key and not dry_run:
            raise ValueError("POLY_PRIVATE_KEY is required")
        if use_deposit_wallet and not deposit_wallet and not dry_run:
            raise ValueError(
                "use_deposit_wallet=True requires deposit_wallet (0x... address). "
                "Set POLY_DEPOSIT_WALLET in .env or pass deposit_wallet=...; the "
                "deposit wallet must be deployed + funded on Polygon mainnet "
                "(factory 0x00000000000Fb5C9ADea0298D729A0CB3823Cc07)."
            )
        self._private_key = private_key
        self._funder = funder or None
        self._host = host
        self._chain_id = chain_id
        self.dry_run = dry_run
        self._use_deposit_wallet = bool(use_deposit_wallet)
        self._deposit_wallet = (deposit_wallet or None)
        self._client = None          # set after initialize()
        self._initialized = False
        self._maker_address: Optional[str] = None  # set after initialize()
        self._sig_type: Optional[int] = None       # set after initialize()

    # ------------------------------------------------------------------
    # Factory
    # ------------------------------------------------------------------

    @classmethod
    def from_env(
        cls,
        env_path: Optional[Path] = None,
        dry_run: bool = False,
        *,
        use_deposit_wallet: Optional[bool] = None,
        deposit_wallet: Optional[str] = None,
    ) -> "CLOBOrderClient":
        """Load credentials from .env file or environment variables.

        Deposit wallet (ERC-1271) opt-in:
          * use_deposit_wallet (kw-only): when None, read POLY_USE_DEPOSIT_WALLET
            env var; truthy strings ("1", "true", "yes", "on") enable the
            sig_type=3 path. CLI flags should pass an explicit bool to override.
          * deposit_wallet (kw-only): when None, read POLY_DEPOSIT_WALLET env var.
            Must be the deterministic deposit-wallet address deployed via the
            Polymarket factory; required when use_deposit_wallet=True.
        """
        if env_path is not None and env_path.exists():
            try:
                from dotenv import load_dotenv
                load_dotenv(env_path)
                LOGGER.info("Loaded .env from %s", env_path)
            except ImportError:
                LOGGER.warning("python-dotenv not installed; reading env vars directly")

        private_key = os.getenv("POLY_PRIVATE_KEY", "")
        funder      = os.getenv("POLY_PUBLIC_KEY", "") or None

        if use_deposit_wallet is None:
            env_flag = os.getenv("POLY_USE_DEPOSIT_WALLET", "").strip().lower()
            use_deposit_wallet = env_flag in {"1", "true", "yes", "on"}
        if deposit_wallet is None:
            deposit_wallet = os.getenv("POLY_DEPOSIT_WALLET", "") or None

        if not private_key and dry_run:
            # Dry-run does not place authenticated orders, so a placeholder key
            # is acceptable and avoids forcing wallet credentials for simulation.
            private_key = "0x" + ("1" * 64)
            LOGGER.warning("POLY_PRIVATE_KEY not found; using placeholder key in dry-run mode.")

        if not private_key:
            raise RuntimeError(
                "POLY_PRIVATE_KEY not found. "
                "Set it in .env or as an environment variable."
            )
        return cls(
            private_key=private_key,
            funder=funder,
            dry_run=dry_run,
            use_deposit_wallet=use_deposit_wallet,
            deposit_wallet=deposit_wallet,
        )

    # ------------------------------------------------------------------
    # Initialization (call once at startup)
    # ------------------------------------------------------------------

    def initialize(self) -> None:
        """Derive API credentials from private key and build the full client.

        Must be called once before any order operations. Raises on failure.
        """
        try:
            from py_clob_client_v2 import ClobClient, ApiCreds
        except ImportError:
            raise RuntimeError(
                "py-clob-client-v2 not installed. Run: pip install py-clob-client-v2"
            )

        if self.dry_run:
            self._initialized = True
            if self._use_deposit_wallet:
                self._sig_type = SIG_TYPE_DEPOSIT_WALLET
                self._maker_address = self._deposit_wallet or "dry_run_deposit_wallet"
                LOGGER.info(
                    "CLOBOrderClient dry-run init (deposit wallet path): "
                    "sig_type=%d  deposit_wallet=%s  V2 SDK import OK, auth skipped.",
                    self._sig_type, self._maker_address,
                )
            else:
                self._sig_type = 0
                self._maker_address = self._funder or "dry_run"
                LOGGER.info("CLOBOrderClient initialized in dry-run mode (V2 SDK import OK, auth skipped).")
            return

        from eth_account import Account
        derived_addr = Account.from_key(self._private_key).address

        # Resolve signature type:
        #   3 = ERC-1271 deposit wallet (opt-in via use_deposit_wallet=True)
        #   2 = legacy proxy/Safe (funder differs from EOA) -- existing default
        #   0 = EOA (no funder configured)
        # The maker_address used for trade-history queries is whichever address
        # holds CLOB buying power (deposit wallet, proxy, or EOA respectively).
        if self._use_deposit_wallet:
            sig_type = SIG_TYPE_DEPOSIT_WALLET
            wallet_funder = self._deposit_wallet
            self._maker_address = self._deposit_wallet
        elif self._funder and self._funder.lower() != derived_addr.lower():
            sig_type = 2
            wallet_funder = self._funder
            self._maker_address = self._funder
        else:
            sig_type = 0
            wallet_funder = self._funder
            self._maker_address = derived_addr
        self._sig_type = sig_type

        if sig_type == SIG_TYPE_DEPOSIT_WALLET:
            LOGGER.info(
                "CLOB init [DEPOSIT WALLET / ERC-1271]: EOA=%s  deposit_wallet=%s  "
                "sig_type=%d  maker_address=%s  dry_run=%s",
                derived_addr, self._deposit_wallet, sig_type, self._maker_address, self.dry_run,
            )
        else:
            LOGGER.info(
                "CLOB init: EOA=%s  funder=%s  sig_type=%d  maker_address=%s  dry_run=%s "
                "(set POLY_USE_DEPOSIT_WALLET=1 + POLY_DEPOSIT_WALLET=0x... to migrate)",
                derived_addr,
                self._funder or "same as EOA",
                sig_type,
                self._maker_address,
                self.dry_run,
            )

        # Step 1: base client to derive API creds
        base = ClobClient(
            host=self._host,
            chain_id=self._chain_id,
            key=self._private_key,
            signature_type=sig_type,
            funder=wallet_funder,
        )

        # Step 2: derive API key from on-chain wallet signature
        api_creds = base.derive_api_key()
        LOGGER.info("API key derived: %s...", api_creds.api_key[:8])

        # Step 3: full authenticated client
        creds = ApiCreds(
            api_key=api_creds.api_key,
            api_secret=api_creds.api_secret,
            api_passphrase=api_creds.api_passphrase,
        )
        self._client = ClobClient(
            host=self._host,
            chain_id=self._chain_id,
            key=self._private_key,
            creds=creds,
            signature_type=sig_type,
            funder=wallet_funder,
        )
        self._initialized = True
        LOGGER.info("CLOBOrderClient initialized OK")

    # ------------------------------------------------------------------
    # Health check
    # ------------------------------------------------------------------

    def health_check(self) -> bool:
        """Lightweight probe to verify the CLOB API is reachable.

        Returns True if the CLOB responds, False on transient error.
        Does NOT require authentication — uses the unauthenticated
        server-time endpoint.
        """
        if self.dry_run:
            return True
        try:
            import httpx
            resp = httpx.get(f"{self._host}/time", timeout=10.0)
            resp.raise_for_status()
            return True
        except Exception as exc:
            LOGGER.debug("health_check failed: %s", exc)
            return False

    # ------------------------------------------------------------------
    # Order operations
    # ------------------------------------------------------------------

    def place_limit_buy(
        self,
        token_id: str,
        price: float,
        size_usdc: float,
    ) -> OrderResult:
        """Place a limit BUY order.

        Args:
            token_id:   Polymarket CLOB token ID for the OVER outcome.
            price:      Limit price (0.01–0.99, two decimal places).
            size_usdc:  Desired maximum USDC cost; converted to CTF shares.

        Returns:
            OrderResult with success flag and order_id if accepted.
        """
        if price <= 0 or price >= 1:
            return OrderResult(success=False, error=f"Invalid buy price: {price}")
        if size_usdc <= 0:
            return OrderResult(success=False, error=f"Invalid size_usdc: {size_usdc}")

        rounded_price = round(price, 2)
        if rounded_price <= 0 or rounded_price >= 1:
            return OrderResult(success=False, error=f"Invalid rounded buy price: {rounded_price}")

        size_shares = size_usdc / rounded_price
        if not math.isfinite(size_shares) or size_shares <= 0:
            return OrderResult(success=False, error=f"Invalid computed share size: {size_shares}")

        if self.dry_run:
            LOGGER.info(
                "DRY-RUN: would BUY %.4f shares @ %.3f (max_cost=$%.2f)  token=%s...%s",
                size_shares, rounded_price, size_usdc, token_id[:12], token_id[-6:],
            )
            return OrderResult(
                success=True,
                order_id=f"dry_run_{int(time.time()*1000)}",
                status="dry_run",
                size_shares=size_shares,
                notional_usdc=size_usdc,
            )

        if not self._initialized or self._client is None:
            return OrderResult(success=False, error="Client not initialized. Call initialize() first.")

        try:
            from py_clob_client_v2 import OrderArgsV2
            from py_clob_client_v2.order_builder import BUY
        except ImportError:
            return OrderResult(success=False, error="py-clob-client-v2 not installed")

        # V2 OrderArgsV2 takes the same 4 required fields as V1 OrderArgs
        # (token_id, price, size, side). The extra V2 fields (expiration,
        # builder_code, metadata) default to safe values; we don't set them.
        order_args = OrderArgsV2(
            token_id=token_id,
            price=rounded_price,
            size=size_shares,
            side=BUY,
        )

        try:
            t_sign = time.time()
            signed = self._client.create_order(order_args)
            sign_ms = (time.time() - t_sign) * 1000.0

            t_post = time.time()
            response = _retry_on_transient(
                self._client.post_order, signed,
                operation_name="post_order",
            )
            post_ms = (time.time() - t_post) * 1000.0

            LOGGER.debug(
                "Order posted: price=%.3f  shares=%.4f  max_cost=$%.2f  "
                "sign=%.0fms  post=%.0fms  resp=%s",
                rounded_price, size_shares, size_usdc, sign_ms, post_ms, response,
            )

            if not isinstance(response, dict):
                return OrderResult(
                    success=False,
                    error=f"Unexpected response type: {type(response).__name__}: {response}",
                    sign_ms=sign_ms,
                    post_ms=post_ms,
                )

            order_id = response.get("orderID") or response.get("id") or response.get("order_id")
            status   = response.get("status", "unknown")

            if order_id:
                LOGGER.info(
                    "Order accepted: id=%s  status=%s  price=%.3f  shares=%.4f  "
                    "max_cost=$%.2f  sign=%.0fms  post=%.0fms",
                    order_id, status, rounded_price, size_shares, size_usdc, sign_ms, post_ms,
                )
                return OrderResult(
                    success=True,
                    order_id=str(order_id),
                    status=str(status),
                    sign_ms=sign_ms,
                    post_ms=post_ms,
                    size_shares=size_shares,
                    notional_usdc=size_usdc,
                )
            else:
                return OrderResult(
                    success=False,
                    error=f"No order_id in response: {response}",
                    sign_ms=sign_ms,
                    post_ms=post_ms,
                )

        except Exception as exc:
            LOGGER.error("place_limit_buy failed: %s", exc)
            return OrderResult(success=False, error=str(exc))

    def cancel_order(self, order_id: str) -> bool:
        """Cancel an open order. Returns True if cancelled or already gone.

        V2 note: the SDK method is now cancel_order(payload: OrderPayload)
        rather than V1's cancel(order_id: str). Per-order cancellation is
        still supported — the migration doc's "operator-controlled
        pauseUser/unpauseUser" wording referred to backend mechanics, not
        the client API. See model_improvements/clob_v2_cp1_findings.txt §3.5.
        """
        if self.dry_run:
            LOGGER.info("DRY-RUN: would cancel order %s", order_id)
            return True

        if not self._initialized or self._client is None:
            LOGGER.error("cancel_order called before initialize()")
            return False

        try:
            from py_clob_client_v2 import OrderPayload
        except ImportError:
            LOGGER.warning("cancel_order: py-clob-client-v2 not installed")
            return False

        try:
            resp = _retry_on_transient(
                self._client.cancel_order, OrderPayload(orderID=order_id),
                operation_name=f"cancel_order({order_id[:12]})",
            )
            LOGGER.info("Cancel order %s: %s", order_id, resp)
            return True
        except Exception as exc:
            LOGGER.warning("cancel_order %s failed: %s", order_id, exc)
            return False

    def get_order(self, order_id: str) -> OrderStatus:
        """Fetch current status of a single order."""
        if self.dry_run:
            return OrderStatus(order_id=order_id, status="dry_run", size_matched=0.0)

        if not self._initialized or self._client is None:
            return OrderStatus(order_id=order_id, status="unknown", error="not initialized")

        try:
            resp = _retry_on_transient(
                self._client.get_order, order_id,
                operation_name=f"get_order({order_id[:12]})",
            )
            if not isinstance(resp, dict):
                return OrderStatus(order_id=order_id, status="unknown", error=str(resp))

            # py_clob_client response fields — normalize status to lowercase
            status       = str(resp.get("status") or "unknown").lower()
            size_matched = float(resp.get("size_matched") or 0.0)
            price        = float(resp.get("price") or 0.0) or None

            return OrderStatus(
                order_id=order_id,
                status=status,
                size_matched=size_matched,
                price=price,
            )
        except Exception as exc:
            LOGGER.warning("get_order %s failed: %s", order_id, exc)
            return OrderStatus(order_id=order_id, status="unknown", error=str(exc))

    def get_trades_for_order(
        self,
        order_id: str,
        token_id: str,
        placed_at_ts: float,
    ) -> Optional[TradeRecord]:
        """Search trade history for a fill matching order_id.

        get_order() is unreliable after market settlement — Polymarket clears
        order records from its CLOB lookup when a market resolves. Trade records
        are permanent. This method is the fallback used when get_order() returns
        "unknown" for an order we believe should be resolved.

        Strategy: query trades filtered by our maker_address + token_id + timestamp
        (since order placement), then match against order_id in the trade fields.
        If no order_id match is found but exactly one trade exists in the window,
        that trade is assumed to be ours (we only ever have one order per token at
        a time per session).

        Args:
            order_id:     The CLOB order ID we're looking for.
            token_id:     The market token ID (over outcome token).
            placed_at_ts: Unix timestamp when the order was placed (for time filter).

        Returns:
            TradeRecord if a matching fill is found, else None.
        """
        if self.dry_run:
            return None

        if not self._initialized or self._client is None:
            LOGGER.warning("get_trades_for_order called before initialize()")
            return None

        if not self._maker_address:
            LOGGER.warning("get_trades_for_order: maker_address not set, cannot query")
            return None

        try:
            from py_clob_client_v2 import TradeParams
        except ImportError:
            LOGGER.warning("get_trades_for_order: py-clob-client-v2 not installed")
            return None

        try:
            params = TradeParams(
                maker_address=self._maker_address,
                market=token_id,
                after=int(placed_at_ts),
            )
            trades = _retry_on_transient(
                self._client.get_trades, params,
                operation_name="get_trades",
            )
        except Exception as exc:
            LOGGER.warning("get_trades_for_order API call failed: %s", exc)
            return None

        if not trades:
            LOGGER.debug(
                "get_trades_for_order: no trades found for token=%s...%s since ts=%d",
                token_id[:10], token_id[-6:], int(placed_at_ts),
            )
            return None

        LOGGER.debug(
            "get_trades_for_order: %d trade(s) found for token=%s...%s since ts=%d",
            len(trades), token_id[:10], token_id[-6:], int(placed_at_ts),
        )

        # Prefer an exact order_id match in known trade fields.
        # Polymarket trade objects may carry the order ID under different field
        # names depending on whether we were maker or taker — check both.
        match = None
        for t in trades:
            if not isinstance(t, dict):
                continue
            if (t.get("maker_order_id") == order_id
                    or t.get("taker_order_id") == order_id
                    or t.get("order_id") == order_id):
                match = t
                break

        # Fallback: if exactly one trade exists in the window and no exact match
        # was found, assume it's ours. We only ever post one order per token per
        # session, so ambiguity is not possible.
        if match is None and len(trades) == 1:
            match = trades[0]
            LOGGER.info(
                "get_trades_for_order: no order_id match — using sole trade in window "
                "(order_id=%s  trade=%s)",
                order_id, match.get("id") or match.get("transaction_hash", "?"),
            )

        if match is None:
            LOGGER.warning(
                "get_trades_for_order: %d trade(s) found but none matched order_id=%s",
                len(trades), order_id,
            )
            return None

        # Parse price and size — API may return strings
        try:
            price = float(match.get("price") or 0.0)
            size  = float(match.get("size") or match.get("size_matched") or 0.0)
        except (TypeError, ValueError) as exc:
            LOGGER.warning("get_trades_for_order: could not parse price/size: %s", exc)
            return None

        if price <= 0 or size <= 0:
            LOGGER.warning(
                "get_trades_for_order: invalid price=%.4f or size=%.4f in trade",
                price, size,
            )
            return None

        # Convert timestamp to ISO string
        raw_ts = match.get("timestamp") or match.get("created_at") or match.get("time")
        timestamp_iso = _ts_to_iso(raw_ts)

        trade_id = str(match.get("id") or match.get("transaction_hash") or "unknown")
        LOGGER.info(
            "get_trades_for_order: fill confirmed  trade_id=%s  price=%.4f  "
            "size=%.2f  timestamp=%s",
            trade_id, price, size, timestamp_iso,
        )
        return TradeRecord(
            trade_id=trade_id,
            price=price,
            size=size,
            timestamp_iso=timestamp_iso,
        )

    # ------------------------------------------------------------------
    # Public data-api lookups (wallet-keyed, no auth required)
    # ------------------------------------------------------------------
    #
    # The CLOB SDK's get_trades / get_order calls filter by maker_address only,
    # which silently drops taker-side fills and any trade Polymarket happened
    # not to index under our wallet as maker. The orphan-fill bug we hit on
    # 2026-05-10 (MIN@CLE 7.5 — bot logged MISSED but the share was in the
    # wallet) traced to exactly this. The data-api endpoints below query by
    # wallet address and return positions + trades regardless of which side
    # of the book we were on.

    def _data_api_wallet(self) -> Optional[str]:
        """Return the wallet whose positions / trades the data-api should query.

        Prefers the deposit wallet (when the sig_type=3 path is active) so the
        query targets whichever address actually clears trades.
        """
        if self._maker_address:
            return self._maker_address
        if self._use_deposit_wallet and self._deposit_wallet:
            return self._deposit_wallet
        return self._funder

    def get_user_positions(
        self,
        *,
        timeout: float = DATA_API_DEFAULT_TIMEOUT_SECS,
    ) -> Optional[dict]:
        """Fetch the wallet's current open positions across all markets.

        Returns ``{token_id: position_size_shares}`` mapping, or ``None`` on
        request failure. Positions reflect actual on-chain holdings, so this
        is the authoritative answer to "did my order really fill" --
        independent of CLOB order-status caching or maker/taker indexing
        quirks.

        Best-effort: any HTTP / parse failure logs a warning and returns
        ``None`` so the caller can fall back to existing reconciliation
        signals without crashing shutdown.
        """
        if self.dry_run:
            return {}

        wallet = self._data_api_wallet()
        if not wallet:
            LOGGER.warning("get_user_positions: wallet address not set; cannot query")
            return None

        import requests
        try:
            resp = requests.get(
                f"{DATA_API_BASE}/positions",
                params={"user": wallet},
                timeout=timeout,
            )
            if resp.status_code != 200:
                LOGGER.warning(
                    "get_user_positions: HTTP %d for wallet=%s", resp.status_code, wallet,
                )
                return None
            payload = resp.json()
        except Exception as exc:
            LOGGER.warning("get_user_positions: request failed: %s", exc)
            return None

        rows: list
        if isinstance(payload, list):
            rows = payload
        elif isinstance(payload, dict):
            rows = payload.get("data") or payload.get("positions") or payload.get("results") or []
        else:
            rows = []

        positions: dict = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            token_id = (
                row.get("asset")
                or row.get("token_id")
                or row.get("tokenId")
                or row.get("conditionId")
                or row.get("market")
            )
            if not token_id:
                continue
            size_raw = (
                row.get("size")
                or row.get("position")
                or row.get("balance")
                or row.get("shares")
                or 0
            )
            try:
                size = float(size_raw)
            except (TypeError, ValueError):
                continue
            if size <= 0:
                continue
            positions[str(token_id)] = positions.get(str(token_id), 0.0) + size

        return positions

    def get_user_trades_for_market(
        self,
        token_id: str,
        *,
        after_ts: Optional[float] = None,
        limit: int = 100,
        timeout: float = DATA_API_DEFAULT_TIMEOUT_SECS,
    ) -> Optional[list]:
        """Fetch this wallet's trades on a single market via the data-api.

        Unlike :meth:`get_trades_for_order` which filters by ``maker_address``
        on the CLOB API, this hits the public data-api with ``user=<wallet>``,
        so it captures both maker and taker fills.

        Returns a list of normalized dicts ``{trade_id, price, size,
        timestamp_iso, side, raw}`` sorted oldest-first, or ``None`` on
        request failure.
        """
        if self.dry_run:
            return []

        wallet = self._data_api_wallet()
        if not wallet:
            LOGGER.warning("get_user_trades_for_market: wallet address not set; cannot query")
            return None

        params = {"user": wallet, "market": token_id, "limit": int(limit)}
        if after_ts is not None:
            params["after"] = int(after_ts)

        import requests
        try:
            resp = requests.get(f"{DATA_API_BASE}/trades", params=params, timeout=timeout)
            if resp.status_code != 200:
                LOGGER.warning(
                    "get_user_trades_for_market: HTTP %d for wallet=%s market=%s...%s",
                    resp.status_code, wallet, token_id[:10], token_id[-6:],
                )
                return None
            payload = resp.json()
        except Exception as exc:
            LOGGER.warning("get_user_trades_for_market: request failed: %s", exc)
            return None

        rows: list
        if isinstance(payload, list):
            rows = payload
        elif isinstance(payload, dict):
            rows = payload.get("data") or payload.get("trades") or payload.get("results") or []
        else:
            rows = []

        trades: list = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            try:
                price = float(row.get("price") or 0.0)
                size = float(row.get("size") or row.get("matched_amount") or row.get("amount") or 0.0)
            except (TypeError, ValueError):
                continue
            if price <= 0 or size <= 0:
                continue
            raw_ts = (
                row.get("timestamp")
                or row.get("matchTime")
                or row.get("created_at")
                or row.get("time")
            )
            ts_iso = _ts_to_iso(raw_ts)
            trade_id = str(
                row.get("id")
                or row.get("trade_id")
                or row.get("transaction_hash")
                or row.get("transactionHash")
                or "unknown"
            )
            trades.append({
                "trade_id": trade_id,
                "price": price,
                "size": size,
                "timestamp_iso": ts_iso,
                "side": row.get("side") or row.get("takerSide"),
                "raw": row,
            })

        # Sort oldest-first when timestamps are parseable; stable otherwise.
        trades.sort(key=lambda t: t.get("timestamp_iso") or "")
        return trades
