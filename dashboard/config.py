import os
from pathlib import Path

DB_PATH = str(Path(__file__).parent.parent / "stock_bot.db")

# Read from algo_trader config or .env if needed
T212_MODE = os.getenv("T212_MODE", "demo")
T212_KEY  = os.getenv("T212_API_KEY", "")

# Mocked config parameters for the dashboard display
STOP_LOSS_PCT     = 0.05
TRAILING_STOP_PCT = 0.03
TRAILING_ACTIVATE = 0.05
TP1_PCT           = 0.05
TP2_PCT           = 0.08
TP3_PCT           = 0.12
MAX_HOLD_HOURS    = 72
POSITION_SIZE_USD = "Dynamic (10%)"

DASHBOARD_CSS = """
<style>
/* Base theme */
.stApp { background-color: #0f172a; color: #f1f5f9; font-family: 'Inter', sans-serif; }
/* Clean up streamlit UI */
header, footer { display: none !important; }
.block-container { padding-top: 1rem !important; padding-bottom: 2rem !important; max-width: 1400px; }
/* Status dots */
.status-dot { height: 10px; width: 10px; border-radius: 50%; display: inline-block; margin-right: 6px; }
.status-running { background-color: #00e6a7; box-shadow: 0 0 8px #00e6a7; }
.status-stopped { background-color: #ff5c5c; box-shadow: 0 0 8px #ff5c5c; }
</style>
"""
