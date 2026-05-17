"""
clob_order_client.py -- Backward-compatible re-export shim.

The Polymarket CLOB client has moved to polymarket_client.py.
This file exists so any code importing from clob_order_client keeps working.
"""
from polymarket_client import *  # noqa: F401,F403
from polymarket_client import CLOBOrderClient
from models import OrderResult, OrderStatus, TradeRecord, _ts_to_iso
