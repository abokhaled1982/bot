"""
src/adapters/binance_orderflow.py — Binance Ticker Adapter

Minimal price feed the copy-trader uses to verify each coin on Binance
Spot before opening a position (24h volume, freshness). The legacy
"OrderFlow" strategy (whale/depth signals) has been removed.

Streams:
  • !miniTicker@arr — All USDT prices + 24h volume, 1s updates
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from typing import Optional

import websockets
from loguru import logger

MIN_VOLUME_24H = float(os.getenv("BN_MIN_VOLUME_24H", "5000000"))
WS_BASE = "wss://stream.binance.com:9443"


class BinanceOrderFlowAdapter:
    """Minimal Binance ticker feed used for copy-trade pre-checks."""

    def __init__(self) -> None:
        self._tickers: dict[str, dict] = {}
        self._connected_mini = False
        self._last_update = 0.0

    def get_ticker(self, symbol: str) -> Optional[dict]:
        return self._tickers.get(symbol)

    def all_tickers(self) -> dict[str, dict]:
        return dict(self._tickers)

    def get_book(self, symbol: str) -> Optional[dict]:
        # Depth stream removed with the old OrderFlow strategy; check_binance_market
        # treats a missing book as "skip spread check".
        return None

    def get_top_pairs(self, n: int = 20) -> list[str]:
        pairs = [
            (sym, td["volume_24h"])
            for sym, td in self._tickers.items()
            if td["volume_24h"] >= MIN_VOLUME_24H
        ]
        pairs.sort(key=lambda x: x[1], reverse=True)
        return [p[0] for p in pairs[:n]]

    def status(self) -> dict:
        return {
            "connected":       self._connected_mini,
            "tracked_symbols": len(self._tickers),
            "last_update":     self._last_update,
            "age_sec":         round(time.time() - self._last_update, 1) if self._last_update else None,
        }

    async def start(self) -> None:
        await self._run_mini_ticker()

    async def _run_mini_ticker(self) -> None:
        url = f"{WS_BASE}/ws/!miniTicker@arr"
        delay = 5
        while True:
            try:
                logger.info("[BINANCE] Connecting mini-ticker stream…")
                async with websockets.connect(url) as ws:
                    self._connected_mini = True
                    delay = 5
                    logger.info("[BINANCE] ✅ Mini-ticker connected")
                    async for raw in ws:
                        await self._handle_mini_ticker(json.loads(raw))
            except Exception as e:
                self._connected_mini = False
                logger.warning(f"[BINANCE] Mini-ticker reconnect in {delay}s: {e}")
                await asyncio.sleep(delay)
                delay = min(delay * 2, 60)

    async def _handle_mini_ticker(self, data: list[dict]) -> None:
        now = time.time()
        self._last_update = now
        for t in data:
            sym = t.get("s", "")
            if not sym.endswith("USDT"):
                continue
            price    = float(t.get("c", 0))
            vol_24h  = float(t.get("q", 0))
            open_24h = float(t.get("o", 0))
            high_24h = float(t.get("h", 0))
            low_24h  = float(t.get("l", 0))
            ch24 = ((price - open_24h) / open_24h * 100) if open_24h else 0

            if price > 0 and vol_24h >= MIN_VOLUME_24H:
                self._tickers[sym] = {
                    "symbol":     sym,
                    "address":    sym,
                    "source":     "BINANCE",
                    "price_usd":  price,
                    "volume_24h": vol_24h,
                    "high_24h":   high_24h,
                    "low_24h":    low_24h,
                    "change_24h": ch24,
                    "updated_at": now,
                }

    async def cleanup_loop(self) -> None:
        while True:
            await asyncio.sleep(60)
            st = self.status()
            logger.debug(
                f"[BINANCE] Status | Connected: {st['connected']} | "
                f"Tickers: {st['tracked_symbols']}"
            )
