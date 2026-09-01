# Hyperliquid Copy-Trader Bot

> **Status:** Standardmaessig Paper Trading (`DRY_RUN=True`). Hyperliquid liefert nur Signale (kein Key noetig); Binance-Spot-Orders gehen nur bei deaktiviertem Paper-Modus tatsaechlich raus.

---

## 📋 Inhaltsverzeichnis

1. [Strategie-Uebersicht](#1-strategie-uebersicht)
2. [Auto-Discovery](#2-auto-discovery)
3. [Dashboard im Detail](#3-dashboard-im-detail)
4. [Copy-Signale und Ausfuehrung](#4-copy-signale-und-ausfuehrung)
5. [Konfiguration (.env)](#5-konfiguration-env)
6. [Starten](#6-starten)
7. [Paper- und Live-Modus](#7-paper--und-live-modus)
8. [Datei-Struktur](#8-datei-struktur)
9. [Hyperliquid API: Rate-Limits, WebSocket & Performance](#9-hyperliquid-api-rate-limits-websocket--performance)

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

Alle Hyperliquid-Requests laufen gedrosselt (`HL_MIN_REQUEST_INTERVAL`) und mit Backoff bei `429`, um Rate-Limits zu vermeiden. Waehrend ein Scan laeuft, zeigt das Dashboard das im Status-Feld `discovering` an (siehe Kapitel 3) — das kann beim ersten Start bzw. nach einem Rescan **30–90 Sekunden** dauern (Grund dafuer in [Kapitel 9](#9-hyperliquid-api-rate-limits-websocket--performance)).

---

## 3. Dashboard im Detail

`streamlit run dashboard.py` oeffnet ein Kontrollzentrum mit 5 Tabs (`dashboard/tabs/copytrader.py`):

### 📊 Übersicht
- Live-KPIs (alle 3s aktualisiert): Verbindungsstatus (`LIVE` / `SUCHT TRADER…` / `CONNECTING`), Anzahl getrackter Trader, offene Positionen + Gesamt-Exposure, Gesamt-PnL (unrealisiert, ueber alle getrackten Trader), Auto-Discovery-Status inkl. naechstem Rescan.
- Waehrend eines Leaderboard-Scans erscheint ein Hinweis-Banner statt eines mehrdeutigen "CONNECTING".
- Tabelle aller offenen Positionen aller getrackten Trader (Coin, Seite, Wert, Entry, Hebel, PnL%, PnL$).
- Letzte 10 Copy-Signale als Live-Feed.
- Eigene Binance-Positionen (`positions.json`) als Rohdaten.

### 🔍 Trader-Suche
- Leaderboard nach Zeitfenster (1 Tag/Woche/Monat/Gesamt) durchsuchen, unabhaengig von den Auto-Discovery-Schwellwerten.
- Filter: Min. Win-Rate, Min. Trades, Max. Ø Haltezeit, Min. Account-Wert, Kandidatenzahl.
- Ergebnisse sortierbar nach PnL, Win-Rate, **Volumen (Liquiditaet)**, Trades oder Account-Wert.
- Fortschrittsbalken waehrend der Suche (Kandidat X/Y), da die Abfrage pro Kandidat gedrosselt ist (siehe Kapitel 9).
- Copy-Trading per Checkbox direkt in der Ergebnistabelle aktivieren/deaktivieren.
- **Explorer-Link** pro Trader (`hl_explorer_url`) zur manuellen Verifikation auf `app.hyperliquid.xyz/explorer`.

### 👥 Meine Trader
- Wallet-Adresse manuell hinzufuegen (sofortiges Tracking + Copy-Trading).
- Uebersicht aller getrackten Trader: Quelle (env/manuell/auto), Account-Wert, Fills, offene Positionen, Exposure, PnL, Explorer-Link.
- Entfernen-Button pro Trader (env-Wallets aus `HL_TRADER_WALLETS` sind geschuetzt und koennen nur per `.env` entfernt werden).

### 📡 Signale
- Voller Signal-Log mit Filter nach Symbol und Signal-Typ (`COPY_OPEN_LONG`, `COPY_CLOSE_LONG`, …).

### ⚙️ Einstellungen
- **Live editierbar:** Polling-Intervall (`HL_POLL_INTERVAL`) und Min. Copy-Groesse (`HL_MIN_COPY_SIZE_USD`) — Aenderungen wirken sofort, ohne Neustart.
- **Nur lesend** (aus `.env`): Auto-Discovery-Status, Max. Trader, Rescan-Intervall, Min. Trades/Win-Rate/Account-Wert, Max. Haltezeit, Signal-TTL, Metrik-Cache-Dauer.

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
HL_METRICS_CACHE_TTL=300        # Sekunden: Cache fuer Win-Rate/Trades/Haltezeit pro Wallet
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
│   │   └── hyperliquid_copytrader.py    Auto-Discovery + Positions-Polling + Signale + WS
│   │
│   ├── bot/
│   │   └── copytrader_pipeline.py       Signal-Verarbeitung + Binance-Ausfuehrung
│   │
│   └── execution/
│       └── binance_executor.py          Binance REST Market- und OCO-Orders
│
└── dashboard/
    ├── config.py                         Farben/CSS fuer das Dashboard
    └── tabs/
        └── copytrader.py                 5 Tabs: Übersicht, Suche, Meine Trader, Signale, Einstellungen
```

---

## 9. Hyperliquid API: Rate-Limits, WebSocket & Performance

Die oeffentliche Hyperliquid-API ist kostenlos und braucht keinen Key, ist dafuer aber pro IP rate-limitiert (offizielle Doku: [Rate limits and user limits](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/rate-limits-and-user-limits)):

- **1200 Weight/Minute** pro IP fuer `info`-Requests.
- `clearinghouseState` (Positions-Poll) kostet nur **Weight 2** — unkritisch, laeuft alle `HL_POLL_INTERVAL` Sekunden pro getracktem Wallet.
- `userFills` (fuer Win-Rate/Trades/Haltezeit bei Discovery & Suche) kostet **20 + 1 pro 20 zurueckgegebene Fills** — bei aktiven Scalpern mit bis zu 2000 Fills sind das **bis zu 120 Weight pro Aufruf**. Schon 10 solcher Calls pro Minute reizen das Budget voll aus.

**Warum die Trader-Suche/Discovery 30–90s dauert:** Jeder Kandidat braucht einen eigenen `userFills`-Call, gedrosselt auf `HL_MIN_REQUEST_INTERVAL` (Standard 2s) Abstand. Bei 15–25 Kandidaten ergibt das allein schon 30–50s; kommt es dazwischen zu `429` (Rate-Limit ueberschritten), wartet der Bot zusaetzlich per Exponential-Backoff. Das ist kein Bug der API, sondern die erwartete Kehrseite eines kostenlosen, oeffentlichen Endpoints.

**Was der Bot deshalb tut:**
1. **Drosselung + Backoff** (`_hl_request`, `HL_MIN_REQUEST_INTERVAL`) — verhindert dauerhafte 429-Sperren.
2. **Metrik-Cache** (`HL_METRICS_CACHE_TTL`, Standard 5 min) — dieselben Top-Leaderboard-Wallets tauchen in fast jeder Suche/jedem Rescan wieder auf; ihre Fill-Metriken werden nicht erneut abgefragt, solange der Cache gueltig ist.
3. **WebSocket statt reinem Polling** — der Bot abonniert `userFills` per getracktem Wallet; kommt eine neue Fill-Meldung rein, wird die Position sofort neu abgefragt statt auf den naechsten Poll-Zyklus zu warten. Ein Resubscribe-Loop (alle 10s) sorgt dafuer, dass auch spaeter hinzugefuegte Wallets (manuell oder per Auto-Discovery-Rescan) auf der bestehenden WS-Verbindung mit-abonniert werden, statt nur beim initialen Verbindungsaufbau.
4. **Fortschrittsanzeige im Dashboard** statt eines scheinbar haengenden Klicks auf "Suchen".

**Es gibt keine bessere Alternative-API fuer Hyperliquid-Trader-Daten** — Hyperliquid ist die einzige Quelle fuer seine eigenen On-Chain-Positionen. Drittanbieter (z. B. Hypurrscan) spiegeln lediglich dieselben Daten mit zusaetzlicher Verzoegerung und ohne offizielle Garantie.

