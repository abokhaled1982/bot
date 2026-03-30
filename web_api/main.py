from fastapi import FastAPI, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import sqlite3
import uvicorn
import os
import json

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class ManualTradeRequest(BaseModel):
    token_address: str
    symbol: str

def get_db_connection():
    db_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'memecoin_bot.db')
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn

@app.get("/api/trades")
async def get_trades():
    conn = get_db_connection()
    trades = conn.execute("SELECT * FROM trades ORDER BY timestamp DESC LIMIT 50").fetchall()
    conn.close()
    return [dict(t) for t in trades]

@app.get("/api/logs")
async def get_logs():
    conn = get_db_connection()
    logs = conn.execute("SELECT * FROM bot_logs ORDER BY timestamp DESC LIMIT 20").fetchall()
    conn.close()
    return [dict(l) for l in logs]

@app.post("/api/control/stop")
async def stop_bot():
    with open("STOP_BOT", "w") as f: f.write("STOP")
    return {"status": "success"}

@app.post("/api/trade/manual")
async def trigger_manual_trade(req: ManualTradeRequest):
    # This writes a trigger file that the main bot loop will pick up
    with open("MANUAL_TRADE", "w") as f: 
        json.dump(req.dict(), f)
    return {"status": "Manual trade trigger queued"}

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
