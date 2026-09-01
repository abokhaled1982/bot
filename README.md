# Binance Orderbook Trading Bot

> **Status:** Standardmaessig Paper Trading (`DRY_RUN=True`). Oeffentliche Binance-Spot-Marktdaten werden in Echtzeit verarbeitet; Orders gehen nur bei deaktiviertem Paper-Modus an Binance.

---

## 📋 Inhaltsverzeichnis

1. [Strategie-Uebersicht](#1-strategie-uebersicht)
2. [Marktueberwachung](#2-marktueberwachung)
3. [Gate-System](#3-gate-system-g1g5)
4. [Kauf und Verkauf](#4-kauf-und-verkauf)
5. [Vor- und Nachteile](#5-vor--und-nachteile)
6. [Konfiguration](#6-konfiguration-env)
7. [Starten](#7-starten)
8. [Paper- und Live-Modus](#8-paper--und-live-modus)
9. [Datei-Struktur](#9-datei-struktur)

---

## 1. Strategie-Uebersicht

### Was ist Order Flow Trading?

Der Bot sucht bei liquiden Binance-Spot-Paaren nach kurzfristigem Kaufdruck. Er kombiniert einen grossen aggressiven Kauf mit einem unausgeglichenen Orderbook und einem nicht-negativen 24h-Trend. Das ist ein regelbasiertes Long-Setup, keine Prognose und keine Renditegarantie.

### Das Kernprinzip

```
Bedingung 1: Ein "Wal" kauft aggressiv >= $50,000 in einem Trade
Bedingung 2: Das Order Book zeigt > 1.5x mehr Käufer als Verkäufer
Bedingung 3: Der 24h-Trend ist nicht negativ
    ────────────────────────────────────────────────
    → Market-BUY + OCO Take-Profit/Stop-Loss im Live-Modus
```

Die Strategie ist ereignisgetrieben: Ein Whale-Buy wird unmittelbar in eine interne Queue geschrieben und dann bewertet. Die tatsaechliche End-to-End-Latenz haengt von Binance-WebSockets, Netzwerk, REST-API und Ausfuehrung ab; sie ist nicht garantiert.

## 2. Marktueberwachung

Der Bot ueberwacht kontinuierlich drei oeffentliche Binance-WebSocket-Streams. Dafuer ist kein API-Key erforderlich.

| Methode | Reaktionszeit | Problem |
|---------|--------------|---------|
| RSI / MACD | Minuten | Zeigen die Vergangenheit, nicht die Zukunft |
| 24h Momentum | Stunden | Viel zu langsam für Scalping |
| **Order Flow (dieses System)** | **<100ms** | Zeigt Absicht in Echtzeit |

---

### Stream 1: `!miniTicker@arr` — Marktüberblick
- Preis, 24h-Aenderung, 24h-Volumen, Hoch und Tief aller USDT-Paare.
- Der Bot nutzt nur Paare ab `BN_MIN_VOLUME_24H` und waehlt die volumenstaerksten `TOP_PAIRS` fuer die tieferen Streams.

```
BTCUSDT → $93,420 | Vol: $2.1B | 24h: -1.6%
ETHUSDT → $1,780  | Vol: $890M | 24h: -1.4%
XRPUSDT → $2.41   | Vol: $420M | 24h: +0.8%  ← positiver Trend
```

### Stream 2: `<sym>@aggTrade` — Whale-Detektor
- Zusammengefasste Trades der ueberwachten Paare.
- `m=False` bedeutet: Der Kaeufer war Taker und nahm Liquiditaet aus dem Ask-Book.
- Ein einzelner Kauf ab `WHALE_THRESHOLD_USDT` erzeugt `WHALE_BUY` und loest die Kandidatenbewertung aus.

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
- Die fuenf besten Bid- und Ask-Preislevel pro ueberwachtem Paar.
- Ein `BOOK_LONG` entsteht, wenn das notionale Bid/Ask-Verhaeltnis mindestens `IMBALANCE_RATIO` erreicht.
- Das Dashboard zeigt Stream-Status, frische Whale- und Book-Signale, Kandidaten und die Marktuebersicht. Es steuert keine Trading-Engine und ersetzt keine Positionsueberwachung.

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
    G1: Liquiditaet?       Vol >= $5M/24h + Daten <= 30s alt
        ↓ OK
    G2: Whale-Signal?      Frischer WHALE_BUY in letzten 30s
        ↓ OK
    G3: Book Imbalance?    Bid/Ask Ratio >= 1.5x
        ↓ OK
    G4: Trendfilter?       24h-Change >= 0%
        ↓ OK
    G5: Position frei?     Aktuelle Pos < MAX_POSITIONS (10)
        ↓ OK
    ✅ MARKET BUY + OCO
```

### G1 — Liquiditaets-Filter
```
Bedingung: 24h-Volumen >= $5,000,000
        Letzte Datenaktualisierung <= 30 Sekunden
```

### G2 — Whale Buy Signal (löst Event aus)
```
Bedingung: Einzelner aggTrade >= $50,000 USDT
           m == False (aggressiver Kauf)
           Signal < 30 Sekunden alt
Warum: Ein institutioneller Akteur kauft aggressiv zum Marktpreis.
       Das bedeutet: Sie wollen jetzt kaufen, egal was es kostet.
       Das ist ein starkes Indiz für eine erwartete Kursbewegung nach oben.
```

### G3 — Order Book Imbalance (Level 2)
```
Bedingung: Bid-Volumen / Ask-Volumen >= 1.5
           Signal < 30 Sekunden alt
Warum: Wenn viele Käufer im Orderbuch stehen und wenige Verkäufer,
       wird der Preis steigen, sobald die Asks aufgebraucht sind.
       Das nennt man "Absorption" — ein klassisches L2-Signal.
```

### G4 — Trendfilter
```
Bedingung: 24h-Change >= 0%
```

### G5 — Positions-Limit & Ausführung
```
Bedingung: len(offene_positionen) < MAX_POSITIONS (Standard: 10)
→ MARKET BUY platzieren
→ OCO SELL setzen (TP + SL gleichzeitig)
```

---

## 4. Kauf und Verkauf

### Kauf (Market Order)
```
Symbol:        XRPUSDT
Einstieg:      $2.4100 (Marktpreis zum Zeitpunkt des Signals)
Position:      $10 USDT (konfigurierbar)
Menge:         10 / 2.41 = 4.149 XRP (gerundet auf Lot-Size)
Ausfuehrung:   Market-Order im Live-Modus; im Paper-Modus nur lokale Simulation
```

### Automatischer Exit (OCO = One-Cancels-Other)
```
Einstieg:      $2.4100
               ────────────────────────────────────────
Take-Profit:   $2.4100 × 1.015 = $2.4462  (+1.5%) ✅
Stop-Loss:     $2.4100 × 0.980 = $2.3618  (-2.0%) 🛑
               ────────────────────────────────────────
Risk/Reward:   1 : 0.75
Im Live-Modus wird eine OCO-Verkaufsorder an Binance gesendet. Erreicht eine Teilorder das Ziel, soll Binance die andere stornieren.
```

Im Paper-Modus wird kein Auftrag an Binance gesendet. Der Bot simuliert den Kauf und ueberwacht danach den Live-Preis: Bei Take-Profit oder Stop-Loss wird ein simulierter Verkauf mit PnL in `binance_orderflow.db` gespeichert und die Position aus `positions.json` entfernt. Das Dashboard zeigt offene Paper-Positionen sowie simulierte Kaeufe und Verkaeufe.

## 5. Vor- und Nachteile

### Vorteile

- Oeffentliche Echtzeitdaten: Fuer Monitoring und Paper-Modus ist kein API-Key noetig.
- Der Bot begrenzt sich auf liquide USDT-Spot-Paare und eine konfigurierbare Zahl paralleler Positionen.
- Aggressiver Kauf, sichtbare Book-Imbalance und Trendfilter muessen gemeinsam vorliegen.
- Eine erfolgreich angelegte OCO-Order kann im Live-Modus Gewinnziel und Verlustbegrenzung abbilden.
- Das Dashboard macht Verbindung, Signale, Kandidaten und Marktdaten sichtbar.

### Nachteile und Risiken

- Sichtbare Limit-Orders koennen vor der Ausfuehrung zurueckgezogen werden. Eine Imbalance ist kein verlaesslicher Preisindikator.
- Ein fester Whale-Schwellenwert ist fuer sehr liquide Paare weniger aussagekraeftig als fuer kleinere Paare.
- Market-Orders koennen Slippage und Gebuehren verursachen; die Paper-Simulation bildet beides nicht ab.
- Der 24h-Trend ist ein grober Filter und schuetzt nicht vor kurzfristigen Umkehrungen.
- Es gibt keine automatische Positions- oder OCO-Reconciliation. Schlaegt das Anlegen einer OCO-Order fehl, kann eine ungesicherte Spot-Position bestehen bleiben.
- Die Strategie ist im Repository nicht historisch gegen Gebuehren, Slippage und verschiedene Marktphasen validiert.

---

## 6. Konfiguration (.env)

```ini
# ── Binance API ──────────────────────────────────────────
# Nur bei DRY_RUN=False erforderlich
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
cd /home/alghobariw/Desktop/temp/bot
source .venv/bin/activate
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
cd /home/alghobariw/Desktop/temp/bot
source .venv/bin/activate
streamlit run dashboard.py
# → http://localhost:8501
```

---

## 8. Paper- und Live-Modus

> [!CAUTION]
> Nur wenn du genau weißt was du tust. Echtes Geld kann verloren gehen.

**Paper-Modus:** Bei `DRY_RUN=True` sind keine Binance-API-Schluessel erforderlich. Der Bot nutzt reale Marktdaten, simuliert Kauf und Exit bei Take-Profit oder Stop-Loss und protokolliert beides lokal.

**Live-Modus:** Bei `DRY_RUN=False` braucht das Binance-Spot-Konto ausreichend USDT sowie `BINANCE_API_KEY` und `BINANCE_SECRET` mit Spot-Trading-Berechtigung. Withdrawal muss deaktiviert bleiben; eine IP-Whitelist ist dringend zu empfehlen.

**Aktivierung:**
```ini
# .env
DRY_RUN=False
BINANCE_POSITION_SIZE_USDT=10   # Klein anfangen!
BINANCE_MAX_POSITIONS=3         # Max 3 gleichzeitig am Anfang
BINANCE_STOP_LOSS_PCT=2.0       # Immer Stop-Loss aktiv lassen
```

---

## 9. Datei-Struktur

```
bot/
├── main.py                          # Entry point
├── .env                             # Konfiguration & API Keys
├── binance_orderflow.db              # SQLite: Trades + Logs
├── positions.json                   # Aktuelle offene Positionen
│
├── src/
│   ├── adapters/
│   │   └── binance_orderflow.py     3 WebSocket-Streams + Event-Queue
│   │
│   ├── bot/
│   │   └── orderflow_pipeline.py    Gate G1-G5 + Event-Loop
│   │
│   └── execution/
│       └── binance_executor.py      Binance REST Market- und OCO-Orders
│
└── dashboard/
    └── tabs/
        └── live_market.py           Live Markt- und Signalsicht
```
