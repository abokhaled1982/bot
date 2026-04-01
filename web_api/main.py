from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import sqlite3
import uvicorn
import os
import sys
import json
import asyncio

sys.path.append(os.path.join(os.path.dirname(os.path.dirname(__file__))))

app = FastAPI(title="Memecoin Bot API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "memecoin_bot.db")
POSITIONS_FILE = os.path.join(os.path.dirname(os.path.dirname(__file__)), "positions.json")


# ── Models ────────────────────────────────────────────────────────────────────
class ManualBuyRequest(BaseModel):
    token_address: str
    amount_usd: float = 0.20

class ManualSellRequest(BaseModel):
    token_address: str
    sell_pct: float = 1.0  # 1.0 = 100%


# ── DB Helpers ────────────────────────────────────────────────────────────────
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


# ── Trades ────────────────────────────────────────────────────────────────────
@app.get("/api/trades")
async def get_trades(limit: int = 50, decision: str = None):
    conn = get_db()
    if decision:
        rows = conn.execute(
            "SELECT * FROM trades WHERE decision LIKE ? ORDER BY timestamp DESC LIMIT ?",
            (f"%{decision}%", limit)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM trades ORDER BY timestamp DESC LIMIT ?", (limit,)
        ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.get("/api/trades/stats")
async def get_trade_stats():
    conn = get_db()
    total   = conn.execute("SELECT COUNT(*) as c FROM trades").fetchone()["c"]
    buys    = conn.execute("SELECT COUNT(*) as c FROM trades WHERE decision LIKE '%BUY%'").fetchone()["c"]
    sells   = conn.execute("SELECT COUNT(*) as c FROM trades WHERE decision LIKE '%SELL%'").fetchone()["c"]
    skips   = conn.execute("SELECT COUNT(*) as c FROM trades WHERE decision IN ('SKIP','HOLD')").fetchone()["c"]

    # Funnel analysis
    data_fail = conn.execute(
        "SELECT COUNT(*) as c FROM trades WHERE funnel_stage='DATA_CHECK'"
    ).fetchone()["c"]
    safety_fail = conn.execute(
        "SELECT COUNT(*) as c FROM trades WHERE funnel_stage='SAFETY_CHECK'"
    ).fetchone()["c"]
    pre_filter_fail = conn.execute(
        "SELECT COUNT(*) as c FROM trades WHERE funnel_stage='PRE_FILTER'"
    ).fetchone()["c"]
    scoring_fail = conn.execute(
        "SELECT COUNT(*) as c FROM trades WHERE funnel_stage='SCORING'"
    ).fetchone()["c"]
    exec_limit = conn.execute(
        "SELECT COUNT(*) as c FROM trades WHERE funnel_stage='EXEC_LIMIT'"
    ).fetchone()["c"]
    bought = conn.execute(
        "SELECT COUNT(*) as c FROM trades WHERE funnel_stage='BUY_EXEC'"
    ).fetchone()["c"]

    conn.close()
    return {
        "total": total, "buys": buys, "sells": sells, "skips": skips,
        "funnel": {
            "data_fail": data_fail,
            "safety_fail": safety_fail,
            "pre_filter_fail": pre_filter_fail,
            "scoring_fail": scoring_fail,
            "exec_limit": exec_limit,
            "bought": bought,
        }
    }


# ── Positions ─────────────────────────────────────────────────────────────────
@app.get("/api/positions")
async def get_positions():
    if os.path.exists(POSITIONS_FILE):
        with open(POSITIONS_FILE) as f:
            return json.load(f)
    return {}


# ── Logs ──────────────────────────────────────────────────────────────────────
@app.get("/api/logs")
async def get_logs(limit: int = 50, level: str = None):
    conn = get_db()
    if level and level != "ALL":
        rows = conn.execute(
            "SELECT * FROM bot_logs WHERE level=? ORDER BY timestamp DESC LIMIT ?",
            (level, limit)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM bot_logs ORDER BY timestamp DESC LIMIT ?", (limit,)
        ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ── Bot Control ───────────────────────────────────────────────────────────────
@app.get("/api/bot/status")
async def bot_status():
    stopped = os.path.exists(
        os.path.join(os.path.dirname(os.path.dirname(__file__)), "STOP_BOT")
    )
    return {"running": not stopped}


@app.post("/api/bot/stop")
async def stop_bot():
    path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "STOP_BOT")
    with open(path, "w") as f:
        f.write("STOP")
    return {"status": "stopped"}


@app.post("/api/bot/start")
async def start_bot():
    path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "STOP_BOT")
    if os.path.exists(path):
        os.remove(path)
    return {"status": "started"}


# ── Manual Trading ────────────────────────────────────────────────────────────
@app.post("/api/trade/buy")
async def manual_buy(req: ManualBuyRequest):
    """Queue a manual buy — writes trigger file for the bot to pick up."""
    trigger_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "MANUAL_TRADE")
    with open(trigger_path, "w") as f:
        json.dump({"action": "BUY", "address": req.token_address, "amount": req.amount_usd}, f)
    return {"status": "queued", "action": "BUY", "address": req.token_address}


@app.post("/api/trade/sell")
async def manual_sell(req: ManualSellRequest):
    """Queue a manual sell."""
    trigger_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "MANUAL_TRADE")
    with open(trigger_path, "w") as f:
        json.dump({"action": "SELL", "address": req.token_address, "sell_pct": req.sell_pct}, f)
    return {"status": "queued", "action": "SELL", "address": req.token_address}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
