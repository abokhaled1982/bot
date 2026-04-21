# 🤖 Binance Order Flow Trading Bot

> **Status:** Paper Trading (DRY_RUN=True) | **Architektur:** Event-Driven, <100ms Reaktionszeit

---

## 📋 Inhaltsverzeichnis

1. [Strategie-Übersicht](#1-strategie-übersicht)
2. [Datenbasis — 3 WebSocket Streams](#2-datenbasis--3-websocket-streams)
3. [Gate-System (G1–G5)](#3-gate-system-g1g5)
4. [Event-Driven Architektur](#4-event-driven-architektur)
5. [Order-Ausführung](#5-order-ausführung)
6. [Konfiguration (.env)](#6-konfiguration-env)
7. [Starten](#7-starten)
8. [Von Paper zu Live](#8-von-paper-zu-live)
9. [Datei-Struktur](#9-datei-struktur)

---

## 1. Strategie-Übersicht

### Was ist Order Flow Trading?

Professionelle Trader beobachten nicht nur den Preis, sondern **wer kauft, wie viel und wie aggressiv**. Wenn ein institutioneller Akteur einen großen Market-Buy platziert, bewegt das den Markt. Unser Bot erkennt diese Bewegungen innerhalb von Millisekunden und springt mit auf.

### Das Kernprinzip

```
Bedingung 1: Ein "Wal" kauft aggressiv > $50,000 in einem Trade
Bedingung 2: Das Order Book zeigt > 1.5x mehr Käufer als Verkäufer
Bedingung 3: Der 24h-Trend ist positiv
    ────────────────────────────────────────────────
    → Sofortiger Market-BUY + OCO Stop-Loss/Take-Profit
```

### Warum Order Flow statt Indikatoren?

| Methode | Reaktionszeit | Problem |
|---------|--------------|---------|
| RSI / MACD | Minuten | Zeigen die Vergangenheit, nicht die Zukunft |
| 24h Momentum | Stunden | Viel zu langsam für Scalping |
| **Order Flow (dieses System)** | **<100ms** | Zeigt Absicht in Echtzeit |

---

## 2. Datenbasis — 3 WebSocket Streams

Alle drei Streams laufen **gleichzeitig** ohne Unterbrechung:

### Stream 1: `!miniTicker@arr` — Marktüberblick
- **Was:** Alle ~284 USDT-Paare, Update jede ~1 Sekunde
- **Enthält:** Preis, 24h-Change, 24h-Volumen, High/Low
- **Wozu:** G1 (Liquiditätscheck) + G4 (Trendcheck)

```
BTCUSDT → $93,420 | Vol: $2.1B | 24h: -1.6%
ETHUSDT → $1,780  | Vol: $890M | 24h: -1.4%
XRPUSDT → $2.41   | Vol: $420M | 24h: +0.8%  ← positiver Trend
```

### Stream 2: `<sym>@aggTrade` — Whale-Detektor
- **Was:** Jeder einzelne Trade auf Top-20-Paaren, in Echtzeit (<10ms Latenz)
- **Enthält:** Preis, Menge, Richtung (BUY oder SELL)
- **Wozu:** G2 (Whale-Signal erkennen)

```
XRPUSDT: $186,746 MARKET-BUY  um 22:01:44.312  → 🐋 WHALE erkannt!
BTCUSDT: $12,000  MARKET-SELL um 22:01:44.891  → ignoriert (zu klein)
SOLUSDT: $67,000  MARKET-BUY  um 22:01:45.102  → 🐋 WHALE erkannt!
```

**Was macht einen Whale aus:**
- `is_buyer_maker = False` → Käufer hat aggressiv zum Marktpreis gekauft (nicht gewartet)
- Handelsvolumen > $50,000 USDT in **einem einzigen** aggTrade
- Das Signal wird sofort in die Event-Queue eingestellt (→ Pipeline reagiert in ms)

### Stream 3: `<sym>@depth5` — Order Book Level 2
- **Was:** Top 5 Bid/Ask-Ebenen auf Top-20-Paaren, bei jeder Änderung
- **Enthält:** Preis und Menge pro Ebene
- **Wozu:** G3 (Kaufdruck messen)

```
XRPUSDT Order Book:
  Bids (Käufer):              Asks (Verkäufer):
  $2.410 → 72,000 XRP        $2.411 → 8,000 XRP
  $2.409 → 51,000 XRP        $2.412 → 12,000 XRP
  $2.408 → 38,000 XRP        $2.413 → 5,000 XRP
  ─────────────────────────────────────────────
  Bid-Vol: $389k             Ask-Vol: $61k
  Ratio = 389k / 61k = 6.4x → STARKES KAUFSIGNAL 📗
```

---

## 3. Gate-System (G1–G5)

Jeder Coin muss **alle 5 Gates** passieren, bevor eine Order platziert wird:

```
Whale-Trade erkannt ($50k+)
        ↓
    G1: Liquidität?        Vol > $5M/24h + Daten < 30s alt
        ↓ OK
    G2: Whale-Signal?      Frischer WHALE_BUY in letzten 30s
        ↓ OK
    G3: Book Imbalance?    Bid/Ask Ratio > 1.5x
        ↓ OK
    G4: Trend positiv?     24h-Change > 0%
        ↓ OK
    G5: Position frei?     Aktuelle Pos < MAX_POSITIONS (10)
        ↓ OK
    ✅ MARKET BUY + OCO
```

### G1 — Liquiditäts-Filter (silent)
```
Bedingung: 24h-Volumen > $5,000,000
           Letzte Datenaktualisierung < 30 Sekunden
Warum: Coins mit wenig Volumen haben große Spreads — 
       Kauf und Verkauf kostet zu viel.
```

### G2 — Whale Buy Signal (löst Event aus)
```
Bedingung: Einzelner aggTrade > $50,000 USDT
           is_buyer_maker == False (aggressiver Kauf)
           Signal < 30 Sekunden alt
Warum: Ein institutioneller Akteur kauft aggressiv zum Marktpreis.
       Das bedeutet: Sie wollen jetzt kaufen, egal was es kostet.
       Das ist ein starkes Indiz für eine erwartete Kursbewegung nach oben.
```

### G3 — Order Book Imbalance (Level 2)
```
Bedingung: Bid-Volumen / Ask-Volumen > 1.5
           Signal < 30 Sekunden alt
Warum: Wenn viele Käufer im Orderbuch stehen und wenige Verkäufer,
       wird der Preis steigen, sobald die Asks aufgebraucht sind.
       Das nennt man "Absorption" — ein klassisches L2-Signal.
```

### G4 — Trend-Bestätigung
```
Bedingung: 24h-Change > 0% (Coin ist heute positiv)
Warum: Wir kaufen NICHT gegen den übergeordneten Trend.
       Ein Whale-Buy in einem fallenden Markt kann ein 
       Market Maker sein, der Liquidität sucht (Falle!).
```

### G5 — Positions-Limit & Ausführung
```
Bedingung: len(offene_positionen) < MAX_POSITIONS (Standard: 10)
→ MARKET BUY platzieren
→ OCO SELL setzen (TP + SL gleichzeitig)
```

---

## 4. Event-Driven Architektur

### Das Problem mit Polling (alt)
```
Bot checkt alle 3s → Whale passiert um 22:01:44 → Bot reagiert um 22:01:47
Verzögerung: bis zu 3 Sekunden → Preis bereits bewegt
```

### Die Lösung: asyncio.Queue (jetzt)
```
Whale-Trade erkannt (22:01:44.312)
    ↓ sofort
signal_queue.put_nowait(signal)    ← <1ms
    ↓
main_loop awaitet queue            ← blockiert bis Signal kommt
    ↓
evaluate_candidate() aufgerufen    ← 22:01:44.315 (3ms später!)
    ↓
MARKET BUY platziert               ← 22:01:44.450 (<150ms nach Whale!)
```

### Datenfluss-Diagramm
```
Binance WebSocket
  ├─ !miniTicker@arr ──────────────────→ _tickers{}  (Preis/Vol)
  ├─ BTCUSDT@aggTrade ─→ Whale? ──→ signal_queue ──→ evaluate()
  ├─ ETHUSDT@aggTrade ─→ Whale? ──→ signal_queue ──→ evaluate()  
  ├─ ...@depth5 ───────────────────────→ _signals[]  (BOOK_LONG)
  └─ ...                                             
                                    BinanceExecutor
                                    └─ POST /api/v3/order (MARKET BUY)
                                    └─ POST /api/v3/order/oco (TP+SL)
```

---

## 5. Order-Ausführung

### Kauf (Market Order)
```
Symbol:        XRPUSDT
Einstieg:      $2.4100 (Marktpreis zum Zeitpunkt des Signals)
Position:      $10 USDT (konfigurierbar)
Menge:         10 / 2.41 = 4.149 XRP (gerundet auf Lot-Size)
Ausführung:    < 500ms nach Signal (Binance REST API)
```

### Automatischer Exit (OCO = One-Cancels-Other)
```
Einstieg:      $2.4100
               ────────────────────────────────────────
Take-Profit:   $2.4100 × 1.015 = $2.4462  (+1.5%) ✅
Stop-Loss:     $2.4100 × 0.980 = $2.3618  (-2.0%) 🛑
               ────────────────────────────────────────
Risk/Reward:   1 : 0.75
Wenn TP erreicht → SL-Order wird automatisch storniert
Wenn SL erreicht → TP-Order wird automatisch storniert
```

---

## 6. Konfiguration (.env)

```ini
# ── Binance API ──────────────────────────────────────────
BINANCE_API_KEY=dein_api_key
BINANCE_SECRET=dein_secret

# ── Modus ────────────────────────────────────────────────
DRY_RUN=True                    # True=Paper, False=Echtes Geld

# ── Position ─────────────────────────────────────────────
BINANCE_POSITION_SIZE_USDT=10   # USD pro Trade
BINANCE_MAX_POSITIONS=10        # Max gleichzeitige Positionen

# ── Exit-Strategie ───────────────────────────────────────
BINANCE_STOP_LOSS_PCT=2.0       # Stop-Loss %
BINANCE_TAKE_PROFIT_PCT=1.5     # Take-Profit %

# ── Whale-Detektor ───────────────────────────────────────
WHALE_THRESHOLD_USDT=50000      # Min. Whale-Trade in USD
IMBALANCE_RATIO=1.5             # Min. Bid/Ask-Ratio für G3

# ── Überwachung ──────────────────────────────────────────
BN_MIN_VOLUME_24H=5000000       # Min. 24h-Volumen ($5M)
TOP_PAIRS=20                    # Wie viele Paare für aggTrade/depth
SIGNAL_TTL=30.0                 # Wie lange ist ein Signal gültig (s)
```

---

## 7. Starten

```bash
# Terminal 1 — Trading Engine
cd ~/Desktop/bot-2
source venv/bin/activate
python3 main.py
```

**Normale Console-Ausgabe:**
```
Binance Order Flow Bot — Event-Driven Whale + Book Imbalance
⚡ Mode: EVENT-DRIVEN (reacts within milliseconds of whale trade)
═══════════════════════════════════════════════════════════════
[ORDERFLOW] Warming up streams (10s)...
[ORDERFLOW] ✅ Mini-ticker connected (284 Paare)
[ORDERFLOW] ✅ Order flow streams active (20 Paare)
[ORDERFLOW] ⚡ Listening for whale trades...

── Status #1 | Tickers:284 | Pairs:20 | Signals:8 | Positions:0/10 ──

[ORDERFLOW] 🐋 BUY XRPUSDT | $186,746 @ $2.41
[XRPUSDT] 🐳+G2✔ 📗+G3✔ G4✖ | Whale BUY $186k | Book 6.4x | Trend: Downtrend -0.3%

[ORDERFLOW] 🐋 BUY SOLUSDT | $95,200 @ $142.30
[SOLUSDT] ✅ ALL GATES | Price:$142.3000 | Whale BUY $95k | Book 3.1x | Uptrend: +2.1%
[EXECUTOR] 📝 DRY-RUN MARKET BUY | SOLUSDT | qty=0.070289 | price≈$142.30 | total≈$10.00
[EXECUTOR] 📝 DRY-RUN OCO | SOLUSDT | TP=$144.43 | SL=$139.45
```

```bash
# Terminal 2 — Dashboard
streamlit run dashboard.py
# → http://localhost:8501
```

---

## 8. Von Paper zu Live

> [!CAUTION]
> Nur wenn du genau weißt was du tust. Echtes Geld kann verloren gehen.

**Voraussetzungen:**
1. USDT-Guthaben auf Binance Spot (mind. `POSITION_SIZE × MAX_POSITIONS`)
2. API-Key hat **Spot Trading** Berechtigung (kein Withdrawal nötig)
3. Bot wurde im Paper-Modus getestet und Trades erscheinen in der History

**Aktivierung:**
```ini
# .env
DRY_RUN=False
BINANCE_POSITION_SIZE_USDT=10   # Klein anfangen!
BINANCE_MAX_POSITIONS=3         # Max 3 gleichzeitig am Anfang
BINANCE_STOP_LOSS_PCT=2.0       # Immer Stop-Loss aktiv lassen
```

**Geprüfter Account-Status:**
```
✅ API Key aktiv | canTrade=True | canWithdraw=True
⚠️  Kein USDT-Guthaben → Bitte USDT aufladen um live zu handeln
    Vorhandene Assets: SOL (~$17), EUR ($0.83), USDC ($0.28)
```

---

## 9. Datei-Struktur

```
bot-2/
├── main.py                          # Entry point
├── .env                             # Konfiguration & API Keys
├── memecoin_bot.db                  # SQLite: Trades + Logs
├── positions.json                   # Aktuelle offene Positionen
│
├── src/
│   ├── adapters/
│   │   ├── binance_orderflow.py     ⭐ 3 WS Streams + Event-Queue
│   │   └── binance_stream.py        Dashboard Mini-Ticker
│   │
│   ├── bot/
│   │   ├── orderflow_pipeline.py    ⭐ Gate G1-G5 + Event-Loop
│   │   └── binance_pipeline.py      (Fallback: 24h Momentum)
│   │
│   └── execution/
│       └── binance_executor.py      ⭐ Binance REST Market+OCO Orders
│
└── dashboard/
    ├── dashboard.py
    └── tabs/
        ├── live_market.py           Live Markt-Übersicht
        ├── history.py               Trade History + Live P/L
        └── positions.py             Offene Positionen
```
