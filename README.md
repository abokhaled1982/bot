# Hyperliquid Copy-Trader Bot

> **Status:** Standardmaessig Paper Trading (`DRY_RUN=True`). Hyperliquid liefert nur Signale (kein Key noetig); Binance-Spot-Orders gehen nur bei deaktiviertem Paper-Modus tatsaechlich raus.

---

## 📋 Inhaltsverzeichnis

1. [Strategie-Uebersicht](#1-strategie-uebersicht)
2. [Auto-Discovery](#2-auto-discovery)
3. [Manuelle Trader-Suche (Dashboard)](#3-manuelle-trader-suche-dashboard)
4. [Copy-Signale und Ausfuehrung](#4-copy-signale-und-ausfuehrung)
5. [Konfiguration (.env)](#5-konfiguration-env)
6. [Starten](#6-starten)
7. [Paper- und Live-Modus](#7-paper--und-live-modus)
8. [Datei-Struktur](#8-datei-struktur)

---

## 1. Strategie-Uebersicht

Der Bot kopiert die Positionen erfolgreicher Trader auf Hyperliquid (vollstaendig on-chain, oeffentliche API) und fuehrt die entsprechenden Trades auf Binance Spot aus. Hyperliquid dient **ausschliesslich als Signalquelle** — es findet dort keine eigene Ausfuehrung statt.

```
Getrackter Trader oeffnet BTC Long auf Hyperliquid
        ↓
    COPY_OPEN_LONG Signal
        ↓
    Binance-Marktcheck: BTCUSDT liquide? Spread ok? Position schon offen?
        ↓ OK
    Market-BUY auf Binance + OCO-Exit (Take-Profit / Stop-Loss)

Getrackter Trader schliesst die Position
        ↓
    COPY_CLOSE_LONG Signal → Market-SELL auf Binance
```

Binance wird dabei weiterhin gebraucht: Der `BinanceOrderFlowAdapter` liefert Live-Ticker und Orderbuch, um vor jeder Kopie Liquiditaet, Spread und Handelbarkeit des Coins zu pruefen (`check_binance_market`).

Getrackte Trader kommen aus zwei Quellen:
- **Auto-Discovery** — der Bot durchsucht das Hyperliquid-Leaderboard automatisch und periodisch (siehe [Kapitel 2](#2-auto-discovery)).
- **Manuelle Auswahl** — du suchst im Dashboard selbst nach Tradern und aktivierst sie gezielt (siehe [Kapitel 3](#3-manuelle-trader-suche-dashboard)).

---

## 2. Auto-Discovery

Ist `HL_AUTO_DISCOVER=True` (Standard), scannt der Bot beim Start und danach alle `HL_RESCAN_HOURS` Stunden das oeffentliche Hyperliquid-Leaderboard nach aktiven Scalpern:

```
1. Leaderboard laden (stats-data.hyperliquid.xyz)
2. Nach 24h-Volumen vorsortieren → Top HL_DISCOVERY_POOL Kandidaten
3. Pro Kandidat die Fills laden und daraus ableiten:
     - Anzahl geschlossener Trades
     - Win-Rate (closedPnl > 0)
     - durchschnittliche Haltezeit (Open → Close pro Coin)
     - letzte Aktivitaet
4. Filtern nach HL_MIN_TRADES, HL_MIN_WIN_RATE, HL_MAX_AVG_HOLD_SEC,
   HL_MIN_ACCOUNT_USD, HL_ACTIVE_WITHIN_HOURS
5. Top HL_MAX_TRADERS nach Win-Rate auswaehlen und tracken
```

Manuell aktivierte Trader (env `HL_TRADER_WALLETS` oder per Dashboard) werden von einem Rescan nie entfernt.

Alle Hyperliquid-Requests laufen gedrosselt (`HL_MIN_REQUEST_INTERVAL`) und mit Backoff bei `429`, um Rate-Limits zu vermeiden.

---

## 3. Manuelle Trader-Suche (Dashboard)

Im Dashboard-Tab kannst du das Leaderboard selbst durchsuchen, unabhaengig von den Auto-Discovery-Schwellwerten:

- **Zeitfenster waehlen:** 1 Tag / 1 Woche / 1 Monat / Gesamt — Ranking nach PnL im gewaehlten Fenster.
- **Filter setzen:** Min. Win-Rate, Min. Trades, Max. durchschnittliche Haltezeit.
- **Suchen** klicken → der Bot laedt das Leaderboard, berechnet pro Kandidat die Fill-Metriken und zeigt eine Ergebnisliste.
- **Aktivieren/Deaktivieren** per Checkbox pro Trader — sobald aktiviert, trackt und kopiert der Bot diesen Trader sofort (naechster Poll-Zyklus, Standard alle 5s), ohne auf den naechsten Auto-Discovery-Scan zu warten.

Aktivierte Trader werden in `hl_active_traders.json` gespeichert und ueberleben einen Bot-Neustart.

---

## 4. Copy-Signale und Ausfuehrung

| Signal | Bedeutung |
|--------|-----------|
| `COPY_OPEN_LONG` | Trader hat eine neue Long-Position eroeffnet → Binance Market-BUY |
| `COPY_CLOSE_LONG` | Trader hat eine Long-Position geschlossen → Binance Market-SELL |
| `COPY_OPEN_SHORT` | Trader ist short gegangen (nur Info, Binance Spot kann nicht shorten) |
| `COPY_INCREASE` | Trader hat die Position um >5% aufgestockt |
| `COPY_DECREASE` | Trader hat die Position um >5% reduziert |

Vor jeder Kopie prueft `check_binance_market`: 24h-Volumen (`BN_MIN_VOLUME_24H`), Datenaktualitaet, Spread (`SCALP_MAX_SPREAD_BPS`). Positionsgroesse ist fix (`BINANCE_POSITION_SIZE_USDT`), begrenzt durch `BINANCE_MAX_POSITIONS`. Exit erfolgt per OCO (`BINANCE_STOP_LOSS_PCT` / `BINANCE_TAKE_PROFIT_PCT`) oder wenn der kopierte Trader selbst schliesst.

Im Paper-Modus wird kein Auftrag an Binance gesendet. Der Bot simuliert Kauf/Verkauf, protokolliert PnL in `binance_orderflow.db` und verwaltet offene Positionen in `positions.json`.

---

## 5. Konfiguration (.env)

```ini
# ── Binance API ──────────────────────────────────────────
# Nur bei DRY_RUN=False erforderlich
BINANCE_API_KEY=dein_api_key
BINANCE_SECRET=dein_secret

# ── Modus ────────────────────────────────────────────────
DRY_RUN=True                    # True=Paper, False=Echtes Geld

# ── Position (Binance-Ausfuehrung) ───────────────────────
BINANCE_POSITION_SIZE_USDT=10   # USD pro Trade
BINANCE_MAX_POSITIONS=10        # Max gleichzeitige Positionen
BINANCE_STOP_LOSS_PCT=2.0
BINANCE_TAKE_PROFIT_PCT=1.5
BN_MIN_VOLUME_24H=5000000       # Min. 24h-Volumen fuer Binance-Markt-Check
SCALP_MAX_SPREAD_BPS=5

# ── Hyperliquid Copy-Trader ───────────────────────────────
HL_TRADER_WALLETS=              # Kommagetrennte Wallets, immer getrackt
HL_POLL_INTERVAL=5              # Sekunden zwischen Positions-Polls
HL_MIN_COPY_SIZE_USD=1000       # Mindestgroesse fuer ein Open-Signal
HL_SIGNAL_TTL=60.0

# ── Auto-Discovery ────────────────────────────────────────
HL_AUTO_DISCOVER=True
HL_MAX_TRADERS=5                # Max. automatisch getrackte Trader
HL_RESCAN_HOURS=6
HL_MIN_TRADES=100
HL_MIN_WIN_RATE=0.55
HL_MIN_ACCOUNT_USD=10000
HL_MAX_AVG_HOLD_SEC=1800        # Bevorzugt Scalper (kurze Haltezeit)
HL_ACTIVE_WITHIN_HOURS=24
HL_DISCOVERY_POOL=25
HL_MIN_REQUEST_INTERVAL=2.0     # Drosselung gegen HL-429-Limits
HL_ACTIVE_WALLETS_FILE=hl_active_traders.json
```

---

## 6. Starten

```bash
# Terminal 1 — Trading Engine
source venv/bin/activate
python3 main.py
```

```bash
# Terminal 2 — Dashboard
source venv/bin/activate
streamlit run dashboard.py
# → http://localhost:8501
```

---

## 7. Paper- und Live-Modus

> [!CAUTION]
> Nur wenn du genau weißt was du tust. Echtes Geld kann verloren gehen.

**Paper-Modus:** Bei `DRY_RUN=True` sind keine Binance-API-Schluessel erforderlich. Der Bot nutzt reale Marktdaten und Hyperliquid-Signale, simuliert Kauf/Verkauf und protokolliert alles lokal.

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

## 8. Datei-Struktur

```
bot/
├── main.py                              # Entry point (Copy-Trader Pipeline)
├── .env                                 # Konfiguration & API Keys
├── binance_orderflow.db                 # SQLite: Trades + Logs
├── positions.json                       # Aktuelle offene Positionen
├── hl_active_traders.json               # Manuell aktivierte Hyperliquid-Trader
│
├── src/
│   ├── adapters/
│   │   ├── binance_orderflow.py         Binance Ticker/Orderbuch (Liquiditaets-Check)
│   │   └── hyperliquid_copytrader.py    Auto-Discovery + Positions-Polling + Signale
│   │
│   ├── bot/
│   │   └── copytrader_pipeline.py       Signal-Verarbeitung + Binance-Ausfuehrung
│   │
│   └── execution/
│       └── binance_executor.py          Binance REST Market- und OCO-Orders
│
└── dashboard/
    └── tabs/
        └── copytrader.py                Trader-Suche, Aktivierung, Live-Status
```

