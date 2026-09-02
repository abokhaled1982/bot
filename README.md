# Binance Copy-Trader Bot

> **Status:** Standardmaessig Paper Trading (`DRY_RUN=True`). Binance liefert Signale ueber sein eigenes (inoffizielles) Futures-Leaderboard; Binance-Spot-Orders gehen nur bei deaktiviertem Paper-Modus tatsaechlich raus.

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
9. [Binance Leaderboard: inoffizielle API & Grenzen](#9-binance-leaderboard-inoffizielle-api--grenzen)

---

## 1. Strategie-Uebersicht

Der Bot findet profitable Intraday-Trader ueber Binances eigenes Copy-Trading-Leaderboard (Futures) und kopiert ihre Positionen auf Binance Spot. Es wird **nur noch Binance** genutzt — sowohl als Signalquelle als auch zur Ausfuehrung, kein zweiter Exchange mehr noetig.

```
Getrackter Trader oeffnet BTC Long (Binance Futures, oeffentlich geteilt)
        ↓
    COPY_OPEN_LONG Signal
        ↓
    Binance-Spot-Marktcheck: BTCUSDT liquide? Spread ok? Position schon offen?
        ↓ OK
    Market-BUY auf Binance Spot + OCO-Exit (Take-Profit / Stop-Loss)

Getrackter Trader schliesst die Position
        ↓
    COPY_CLOSE_LONG Signal → Market-SELL auf Binance Spot
```

Der `BinanceOrderFlowAdapter` liefert weiterhin Live-Ticker, um vor jeder Kopie Liquiditaet, Spread und Handelbarkeit des Coins zu pruefen (`check_binance_market`).

Getrackte Trader kommen aus zwei Quellen:
- **Auto-Discovery** — der Bot durchsucht das Binance-Leaderboard automatisch und periodisch nach guten Intraday-Tradern (siehe [Kapitel 2](#2-auto-discovery)).
- **Manuelle Auswahl** — du suchst im Dashboard-Scanner selbst nach Tradern und aktivierst sie gezielt (siehe [Kapitel 3](#3-dashboard-im-detail)).

---

## 2. Auto-Discovery

Ist `BNLB_AUTO_DISCOVER=True` (Standard), scannt der Bot beim Start und danach alle `BNLB_RESCAN_HOURS` Stunden Binances oeffentliches Leaderboard nach Tradern, die **innerhalb eines Tages mit gutem Profit handeln**:

```
1. Tages-Leaderboard laden (periodType=DAILY, ROI-sortiert) → Kandidaten-Pool
2. Denselben Pool zusaetzlich nach PNL sowie 7-Tage-/30-Tage-Ranglisten abfragen,
   um Konsistenz zu pruefen (keine Eintagsfliegen)
3. Filtern nach BNLB_MIN_DAY_ROI_PCT, BNLB_MIN_DAY_PNL_USD,
   BNLB_MIN_FOLLOWERS, positiver 7T-/30T-Performance, Positionen oeffentlich
   geteilt, innerhalb BNLB_ACTIVE_WITHIN_HOURS aktiv
4. Top BNLB_MAX_TRADERS nach Quality-Score auswaehlen und tracken
```

Manuell aktivierte Trader (env `BNLB_TRADER_UIDS` oder per Dashboard) werden von einem Rescan nie entfernt.

Alle Binance-Leaderboard-Requests laufen gedrosselt (`BNLB_MIN_REQUEST_INTERVAL`) und mit Backoff bei `429`, um Rate-Limits zu vermeiden (siehe [Kapitel 9](#9-binance-leaderboard-inoffizielle-api--grenzen)).

---

## 3. Dashboard im Detail

`streamlit run dashboard.py` oeffnet ein Kontrollzentrum mit 4 Tabs (`dashboard/tabs/copytrader.py`):

### 🏠 Übersicht
- Live-KPIs (alle 3s aktualisiert): Binance-Guthaben, eigene offene Trades, realisierter PnL, Anzahl kopierter Trader.
- Bot-Prozess-Status, Tracking-Status, letzter Trader-Fill.
- Eigene offene Trades + Live-Feed der Pipeline-Entscheidungen.

### 👥 Meine Trader
- Trader-UID manuell hinzufuegen (sofortiges Tracking + Copy-Trading).
- Uebersicht aller getrackten Trader: Betrag/Trade, eigener PnL, offene Trades, Trader-uPnL, Tracking-Status.
- Details je Trader: eigene Trades, Tracking-Nachweis, aktuelle Trader-Positionen (falls oeffentlich geteilt), Scanner-Verifikation.
- Entfernen-Button pro Trader (env-UIDs aus `BNLB_TRADER_UIDS` sind geschuetzt und koennen nur per `.env` entfernt werden).

### 🔍 Scanner
- Findet Trader, die **innerhalb eines Tages mit gutem Profit** handeln (Tages-ROI/-PnL-Schwellen), geprueft gegen 7-Tage-/30-Tage-Konsistenz.
- Filter: Min. Tages-ROI, Min. Tages-PnL, Min. Follower, Max. Ergebnisse.
- Ergebnisse sortierbar nach Quality-Score, Tages-ROI, Tages-PnL oder Follower.
- Link zum oeffentlichen Binance-Leaderboard-Profil pro Trader zur manuellen Gegenpruefung.
- Copy-Trading per Formular direkt uebernehmen.

### ⚙️ Setup
- Bot-Prozess-Status, Konfiguration aus der `.env` (nur lesend), Pipeline-Protokoll zum Debuggen.

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

Positionen eines Traders sind nur sichtbar, wenn er sie auf Binance oeffentlich teilt (`positionShared`). Ohne geteilte Positionen kann der Bot nur die Leaderboard-Kennzahlen sehen, aber keine Live-Signale fuer diesen Trader erzeugen.

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

# ── Binance-Leaderboard Copy-Trader ───────────────────────
BNLB_TRADER_UIDS=               # Kommagetrennte encryptedUids, immer getrackt
BNLB_POLL_INTERVAL=5            # Sekunden zwischen Positions-Polls
BNLB_MIN_COPY_SIZE_USD=1000     # Mindestgroesse fuer ein Open-Signal
BNLB_SIGNAL_TTL=60.0

# ── Auto-Discovery ("Trader finden, die innerhalb eines Tages
#    mit gutem Profit handeln") ───────────────────────────
BNLB_AUTO_DISCOVER=True
BNLB_MAX_TRADERS=5               # Max. automatisch getrackte Trader
BNLB_RESCAN_HOURS=6
BNLB_MIN_DAY_ROI_PCT=3.0         # Mindest-Tages-ROI in %
BNLB_MIN_DAY_PNL_USD=50          # Mindest-Tages-PnL in USD
BNLB_MIN_FOLLOWERS=0
BNLB_REQUIRE_POSITION_SHARED=True
BNLB_REQUIRE_POSITIVE_WEEK=True
BNLB_REQUIRE_POSITIVE_MONTH=True
BNLB_ACTIVE_WITHIN_HOURS=24
BNLB_DISCOVERY_POOL=50
BNLB_MIN_REQUEST_INTERVAL=1.0    # Drosselung gegen Rate-Limits
BNLB_METRICS_CACHE_TTL=300
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

```bash
# Optional — Trader ohne Dashboard direkt in der Konsole finden
source venv/bin/activate
python3 find_traders.py
```

---

## 7. Paper- und Live-Modus

> [!CAUTION]
> Nur wenn du genau weißt was du tust. Echtes Geld kann verloren gehen.

**Paper-Modus:** Bei `DRY_RUN=True` sind keine Binance-API-Schluessel erforderlich. Der Bot nutzt reale Marktdaten und Binance-Leaderboard-Signale, simuliert Kauf/Verkauf und protokolliert alles lokal.

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
├── find_traders.py                      # CLI: Intraday-Trader ohne Dashboard finden
├── .env                                 # Konfiguration & API Keys
├── binance_orderflow.db                 # SQLite: Trades + Logs
├── positions.json                       # Aktuelle offene Positionen
│
├── src/
│   ├── adapters/
│   │   ├── binance_orderflow.py         Binance Ticker/Orderbuch (Liquiditaets-Check)
│   │   └── binance_leaderboard.py       Auto-Discovery + Positions-Polling + Signale
│   │
│   ├── bot/
│   │   └── copytrader_pipeline.py       Signal-Verarbeitung + Binance-Ausfuehrung
│   │
│   └── execution/
│       └── binance_executor.py          Binance REST Market- und OCO-Orders
│
├── tests/
│   └── test_binance_leaderboard.py      Tests fuer Trader-Finder/Filter-Logik
│
└── dashboard/
    ├── config.py                         Farben/CSS fuer das Dashboard
    └── tabs/
        └── copytrader.py                 4 Tabs: Übersicht, Meine Trader, Scanner, Setup
```

---

## 9. Binance Leaderboard: inoffizielle API & Grenzen

Binance veroeffentlicht fuer sein Copy-Trading-Leaderboard **kein offizielles, dokumentiertes API**. `src/adapters/binance_leaderboard.py` nutzt dieselben `bapi`-Endpunkte, die `binance.com/*/futures-activities/leaderboard` im Browser aufruft:

- `getLeaderboardRank` — Top-Trader je Zeitfenster (Tag/Woche/Monat) und Metrik (ROI/PnL). Gut dokumentiert durch zahlreiche Community-Projekte, deshalb die zuverlaessigste Datenquelle.
- `getOtherPosition` — aktuell offene Positionen eines Traders, **nur wenn er sie oeffentlich geteilt hat** (`positionShared=true`). Ohne Sharing gibt es fuer diesen Trader keine Live-Copy-Signale.

**Wichtige Einschraenkung gegenueber der fruehreren Hyperliquid-Anbindung:** Binance liefert **keine** oeffentliche Trade-fuer-Trade-Historie (kein Aequivalent zu Hyperliquids `userFills`). Kennzahlen wie Profit-Faktor, Drawdown, durchschnittliche Haltezeit oder Win-Rate pro Einzeltrade lassen sich deshalb **nicht** berechnen. Die Verifikation stuetzt sich stattdessen auf das, was Binance tatsaechlich oeffentlich macht: Tages-/Wochen-/Monats-ROI und -PnL, Follower-Zahl und ob Positionen geteilt werden.

**Da die API inoffiziell ist:**
- Sie kann sich jederzeit ohne Ankuendigung aendern — Feld-Extraktion ist bewusst defensiv (`.get()` mit Fallbacks), stuerzt bei fehlenden Feldern nicht ab, liefert dann aber ggf. leere Ergebnisse.
- Es gibt kein SLA und kein offizielles Rate-Limit-Dokument. `BNLB_MIN_REQUEST_INTERVAL` drosselt defensiv, Backoff greift bei `429`.
- Es gibt keine oeffentliche WebSocket-API fuer fremde Trader-Positionen — Tracking laeuft ausschliesslich per Polling (`BNLB_POLL_INTERVAL`).

**Was der Bot deshalb tut:**
1. **Drosselung + Backoff** (`_bn_request`, `BNLB_MIN_REQUEST_INTERVAL`) — verhindert dauerhafte 429-Sperren.
2. **Konsistenzpruefung ueber 3 Zeitfenster** (Tag/Woche/Monat) statt eines einzelnen guten Tages, um Eintagsfliegen auszufiltern.
3. **Defensive Feld-Extraktion** mit mehreren moeglichen Schluesselnamen, damit kleinere API-Aenderungen nicht sofort zu Abstuerzen fuehren.

