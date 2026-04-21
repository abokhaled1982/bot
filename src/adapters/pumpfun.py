"""
PumpPortal WebSocket Adapter
Connects to wss://pumpportal.fun/api/data for real-time memecoin discovery.

Two key signals:
1. subscribeNewToken    — every new token created on Pump.fun
2. subscribeMigration   — when a token graduates from bonding curve to Raydium
                          (THIS is the strongest early buy signal)
"""

import asyncio
import json
import time
from collections import deque
from loguru import logger

import websockets

WS_URL = "wss://pumpportal.fun/api/data"

# How many candidates to keep in the queue
MAX_QUEUE_SIZE     = 100
# Minimum market cap on Pump.fun before we care (in SOL)
MIN_VSOLANA_AMOUNT = 0.5
# Reconnect delay after disconnect
RECONNECT_DELAY    = 5


class PumpFunAdapter:
    def __init__(self):
        # Migration queue — tokens that graduated to Raydium (HIGH priority)
        self.migration_queue: deque = deque(maxlen=MAX_QUEUE_SIZE)
        # New token queue — freshly created (LOWER priority, most will die)
        self.new_token_queue: deque = deque(maxlen=MAX_QUEUE_SIZE)
        # Track seen addresses to avoid duplicates
        self._seen_migrations: set = set()
        self._seen_new: set = set()
        # Stats
        self.total_new_tokens = 0
        self.total_migrations = 0
        self.connected = False
        self._ws = None

    # ── Get candidates for the main loop ──────────────────────────────────────
    def get_migration_candidates(self, limit: int = 10) -> list:
        """
        Pop migration candidates (highest priority).
        These tokens just graduated from Pump.fun to Raydium —
        they now have real liquidity and are tradeable on Jupiter.
        """
        candidates = []
        while self.migration_queue and len(candidates) < limit:
            candidates.append(self.migration_queue.popleft())
        return candidates

    def get_new_token_candidates(self, limit: int = 5) -> list:
        """
        Pop new token candidates (lower priority).
        These are freshly created — most will die, but some will moon.
        Only useful if you want to be EXTREMELY early.
        """
        candidates = []
        while self.new_token_queue and len(candidates) < limit:
            candidates.append(self.new_token_queue.popleft())
        return candidates

    def get_all_candidates(self) -> list:
        """Get migrations first (priority), then new tokens."""
        migrations = self.get_migration_candidates(limit=10)
        new_tokens = self.get_new_token_candidates(limit=5)
        return migrations + new_tokens

    # ── WebSocket connection ──────────────────────────────────────────────────
    async def start(self):
        """Start the WebSocket listener. Runs forever with auto-reconnect."""
        while True:
            try:
                await self._connect()
            except Exception as e:
                logger.error(f"[PUMP.FUN] WebSocket error: {e}")
                self.connected = False
            logger.warning(f"[PUMP.FUN] Reconnecting in {RECONNECT_DELAY}s...")
            await asyncio.sleep(RECONNECT_DELAY)

    async def _connect(self):
        """Connect to PumpPortal and subscribe to events."""
        logger.info(f"[PUMP.FUN] Connecting to {WS_URL}...")

        async with websockets.connect(
            WS_URL,
            ping_interval=20,
            ping_timeout=10,
            close_timeout=5,
        ) as ws:
            self._ws = ws
            self.connected = True
            logger.success("[PUMP.FUN] Connected!")

            # Subscribe to both channels (single connection, multiple subs)
            await ws.send(json.dumps({"method": "subscribeNewToken"}))
            await ws.send(json.dumps({"method": "subscribeMigration"}))
            logger.info("[PUMP.FUN] Subscribed to: newToken + migration")

            async for message in ws:
                try:
                    data = json.loads(message)
                    await self._handle_message(data)
                except json.JSONDecodeError:
                    continue
                except Exception as e:
                    logger.error(f"[PUMP.FUN] Message handling error: {e}")

        self.connected = False

    # ── Message handling ──────────────────────────────────────────────────────
    async def _handle_message(self, data: dict):
        """Route incoming WebSocket messages to the right handler."""

        # Migration event — token graduated to Raydium
        if "mint" in data and "pool" in data:
            await self._handle_migration(data)
            return

        # New token creation event
        if "mint" in data and "traderPublicKey" in data:
            await self._handle_new_token(data)
            return

    async def _handle_migration(self, data: dict):
        """
        Handle migration event: token graduated from Pump.fun bonding curve
        to Raydium. This means it now has REAL liquidity and is tradeable
        on Jupiter. This is the strongest early buy signal.
        """
        mint = data.get("mint", "")
        if not mint or mint in self._seen_migrations:
            return

        self._seen_migrations.add(mint)
        self.total_migrations += 1

        candidate = {
            "address":   mint,
            "symbol":    data.get("symbol", data.get("name", "?")),
            "source":    "pumpfun_migration",
            "pool":      data.get("pool", ""),
            "timestamp": time.time(),
            # Migration-specific data
            "migrated":             True,
            # PumpPortal raw data (for timeline tracking)
            "pumpfun_name":         data.get("name", ""),
            "pumpfun_symbol":       data.get("symbol", ""),
            "pumpfun_uri":          data.get("uri", ""),
            "pumpfun_market_cap":   float(data.get("marketCapSol", 0) or 0),
            "pumpfun_v_sol":        float(data.get("vSolInBondingCurve", 0) or 0),
            "pumpfun_detected_at":  time.time(),  # when WE saw it
        }

        self.migration_queue.append(candidate)
        logger.info(
            f"[PUMP.FUN] MIGRATION #{self.total_migrations}: "
            f"{candidate['symbol']} ({mint[:16]}...) → Raydium pool: {candidate['pool'][:16]}..."
        )

    async def _handle_new_token(self, data: dict):
        """
        Handle new token creation. Most will die within minutes,
        but tracking them lets us watch for early momentum.
        """
        mint = data.get("mint", "")
        if not mint or mint in self._seen_new:
            return

        self._seen_new.add(mint)
        self.total_new_tokens += 1

        # Only queue tokens that have some initial buy activity
        initial_buy = float(data.get("vSolInBondingCurve", 0) or 0)
        if initial_buy < MIN_VSOLANA_AMOUNT:
            return

        candidate = {
            "address":              mint,
            "symbol":               data.get("symbol", data.get("name", "?")),
            "source":               "pumpfun_new",
            "timestamp":            time.time(),
            "migrated":             False,
            # Pump.fun specific data
            "creator":              data.get("traderPublicKey", ""),
            "initial_buy_sol":      initial_buy,
            "market_cap_sol":       float(data.get("marketCapSol", 0) or 0),
            "v_sol_in_bonding":     initial_buy,
            # Timeline tracking
            "pumpfun_detected_at":  time.time(),
        }

        self.new_token_queue.append(candidate)

        # Only log occasionally to avoid spam (thousands of tokens per hour)
        if self.total_new_tokens % 50 == 0:
            logger.info(
                f"[PUMP.FUN] New tokens tracked: {self.total_new_tokens} "
                f"(queue: {len(self.new_token_queue)} new, {len(self.migration_queue)} migrations)"
            )

    # ── Cleanup old seen addresses periodically ──────────────────────────────
    async def cleanup_loop(self, interval: int = 3600):
        """Clear old seen addresses every hour to prevent memory buildup."""
        while True:
            await asyncio.sleep(interval)
            old_size_m = len(self._seen_migrations)
            old_size_n = len(self._seen_new)
            self._seen_migrations.clear()
            self._seen_new.clear()
            logger.info(
                f"[PUMP.FUN] Cleanup: cleared {old_size_m} migrations + {old_size_n} new token addresses"
            )

    # ── Status ────────────────────────────────────────────────────────────────
    def status(self) -> dict:
        return {
            "connected":        self.connected,
            "total_new_tokens": self.total_new_tokens,
            "total_migrations": self.total_migrations,
            "queue_new":        len(self.new_token_queue),
            "queue_migrations": len(self.migration_queue),
        }
