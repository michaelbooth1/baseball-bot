"""
real_trader.py -- Backward-compatible entry point shim.

Core logic has moved to live_engine.py.
This file exists so existing CLI invocations keep working:
    python real_trader.py --dry-run ...
"""
from live_engine import *  # noqa: F401,F403
from live_engine import main, LiveTradingEngine, LiveBetRecord, parse_live_args

# Old names as aliases for any code that imported them directly
RealTradingEngine = LiveTradingEngine
LiveBet = LiveBetRecord

if __name__ == "__main__":
    main()
