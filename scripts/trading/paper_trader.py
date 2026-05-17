"""
paper_trader.py -- Backward-compatible entry point shim.

Core logic has moved to signal_engine.py.
This file exists so existing CLI invocations keep working:
    python paper_trader.py --date 2026-04-22 ...
"""
from signal_engine import *  # noqa: F401,F403
from signal_engine import main, SignalEngine, BetRecord, LineState, parse_trade_args

# Old names as aliases for any code that imported them directly
PaperTradingEngine = SignalEngine
PaperBet = BetRecord

if __name__ == "__main__":
    main()
