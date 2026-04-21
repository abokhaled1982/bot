# 📊 Binance Trading Bot — Strategy & Architecture

## Overview
This bot continuously scans all USDT spot pairs on Binance (approx. 194 pairs) in real-time to identify high-probability momentum breakouts. It uses a 6-gate filtering system to evaluate each coin before executing a trade.

- **Data Source:** Binance WebSocket (`!miniTicker@arr`) — Real-time updates every ~1 second.
- **Scan Interval:** Every 5 seconds.
- **Execution Mode:** Configurable via `DRY_RUN` (True = Paper Trading, False = Live Trading).
- **Position Sizing:** Configurable USDT amount per trade (e.g., $10 USDT).
- **Risk Management:** Hardcoded Stop-Loss and Take-Profit logic.

---

## The 6-Gate Filtering System (G1 - G6)

The bot evaluates each coin through a strict sequence of gates. If a coin fails any gate, it is immediately discarded for that scan.

### 🟦 G1: Market Data Validation
**Goal:** Ensure we have valid, liquid, and fresh data.
- **Minimum 24h Volume:** ≥ $500,000 USDT.
- **Minimum Price:** ≥ $0.000001 (Filters out extreme dust).
- **Data Freshness:** Last update must be < 30 seconds old.

### 🟦 G2: Noise Filter
**Goal:** Prevent buying into extreme, unsustainable pumps or crashing markets.
- **Pump Filter:** 5-minute price change must be < +15%. (If it's already up 20% in 5 minutes, we are too late and risk buying the top).
- **Crash Filter:** 24h price change must be > -20%. (Avoid catching falling knives).
- **Volatility Filter:** The spread between 24h High and 24h Low must be < 50%. (Avoid highly erratic, low-liquidity coins).

### 🟦 G3: Technical Confirmation
**Goal:** Confirm the coin is technically in a positive trend.
- **Trend Check:** The 24h price change must be > 0% (Positive territory).
- **Volume Base:** Ensures the volume tier score calculation has a solid baseline.
*(Note: Phase 2 will introduce RSI, MACD, and SMA checks here).*

### 🟦 G4: Momentum Filter
**Goal:** Catch the "sweet spot" of momentum — moving fast, but not *too* fast.
- **Minimum Momentum:** The short-term trend (currently using 24h proxy) must be ≥ +1.0%.
- **Maximum Momentum:** Must be ≤ +8.0%. This is crucial: if momentum exceeds 8%, the risk of immediate profit-taking (a dump) is too high. We want steady, building momentum, not FOMO spikes.

### 🟦 G5: Binance Fusion Score (0–100)
**Goal:** Rank the surviving candidates to pick the absolute best one.
The score is heavily weighted towards strong 24h trends and high liquidity.

| Component | Weight | Description |
|-----------|--------|-------------|
| **Momentum** | 60% (up to 60 pts) | Derived from the 24h change (change_24h * 3). |
| **Volume Tier** | 40% (up to 40 pts) | Higher volume = higher score. Ranges from 5 pts (<$1M) to 40 pts (≥$500M). |

**Decision Matrix:**
- **Score ≥ 60:** `BUY`
- **Score 40–59:** `HOLD`
- **Score < 40:** `SKIP`

### 🟦 G6: Execution Limits
**Goal:** Portfolio and risk management before final execution.
- **Max Open Positions:** Must be less than the configured limit (e.g., 10).
- **Duplicate Check:** Ensure we don't already hold an active position in this coin.

---

## Trade Flow Execution

1. **WebSocket Stream:** Receives data for all ~194 USDT pairs every second.
2. **Pre-sorting:** Calculates a quick momentum score and sorts the top candidates.
3. **Pipeline Scan (Every 5s):** Passes the top candidates through G1 → G6.
4. **Execution:** If a coin passes G6, a position is opened in the SQLite database (`memecoin_bot.db`). If `DRY_RUN=True`, it only logs the trade.

---

## Configuration (`.env`)

```ini
DRY_RUN=True                      # Set to False to enable live Binance API trading
BINANCE_POSITION_SIZE_USDT=10.0   # Trade size per position
BINANCE_MAX_POSITIONS=10          # Maximum concurrent trades
BINANCE_STOP_LOSS_PCT=5.0         # Hard stop-loss percentage
BINANCE_TAKE_PROFIT_PCT=15.0      # Take-profit percentage

BN_MIN_VOLUME_24H=500000          # G1: Minimum $500k 24h volume
BN_MOMENTUM_MIN=1.0               # G4: Minimum 24h change threshold
BN_BUY_SCORE=60.0                 # G5: Score required to trigger a BUY
```
