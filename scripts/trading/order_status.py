#!/usr/bin/env python3
"""
order_status.py -- Shared live-order status normalization helpers.

Polymarket and SDK paths can emit small spelling/status variants. Keep the
runtime on one interpretation so placement, lifecycle polling, resume, budget
accounting, and diagnostics do not drift apart.
"""

from __future__ import annotations

from typing import Optional


EXPOSURE_COUNTED_ORDER_STATUSES = frozenset({
    "pending",
    "live",
    "delayed",
    "matched",
    "open",
    "unmatched",
})

UNFILLED_TERMINAL_ORDER_STATUSES = frozenset({
    "cancelled",
    "expired",
    "error",
})


def normalize_order_status(status: Optional[str]) -> str:
    """Lower-case status text and collapse known spelling variants."""
    raw = str(status or "").strip().lower()
    if raw == "canceled":
        return "cancelled"
    return raw


def is_exposure_counted_status(status: Optional[str]) -> bool:
    """True for unresolved orders that still reserve budget/exposure."""
    return normalize_order_status(status) in EXPOSURE_COUNTED_ORDER_STATUSES


def is_unfilled_terminal_status(status: Optional[str]) -> bool:
    """True for terminal statuses that did not create a live filled position."""
    return normalize_order_status(status) in UNFILLED_TERMINAL_ORDER_STATUSES


def is_poll_filled_status(status: Optional[str]) -> bool:
    """True when CLOB polling indicates the order has matched/filled."""
    return normalize_order_status(status) in {"filled", "matched"}


def normalize_accepted_order_status(status: Optional[str]) -> str:
    """Map accepted-but-unresolved placement statuses into live exposure.

    At placement time, statuses such as matched/delayed/open mean the order was
    accepted and needs lifecycle tracking. Fill details are still confirmed by
    polling/reconciliation, so these normalize into the exposure-counted bucket.
    """
    norm = normalize_order_status(status)
    if not norm or norm == "unknown" or norm in EXPOSURE_COUNTED_ORDER_STATUSES:
        return "live"
    if norm in {"filled", "complete", "completed"}:
        return "live"
    return norm
