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

Der Bot sucht bei liquiden Binance-Spot-Paaren nach kurzfristigem Kaufdruck. Ein Einstieg verlangt mehrere unabhaengige Bestaetigungen im selben Sekundenfenster statt eines einzelnen Whale-Prints plus 24h-Wert. Das ist ein regelbasiertes Long-Setup, keine Prognose und keine Renditegarantie.

### Das Kernprinzip

```
Bedingung 1: Ein "Wal" kauft aggressiv >= $50,000 in einem Trade
Bedingung 2: Bid-Dominanz haelt ueber viele Orderbuch-Snapshots an
Bedingung 3: Aggressives Kaufvolumen schlaegt Verkaufsvolumen (>= 1.6x)
Bedingung 4: Der Preis tickt im Sekundenfenster bereits nach oben
Bedingung 5: Die Bid-Wand bleibt bestehen; keine Spoofing-Warnung
Bedingung 6: Spread eng genug, Buch tief genug und Zielbewegung erreichbar
    ────────────────────────────────────────────────
    → Market-BUY + Take-Profit / Stop-Loss / Trailing / Zeit-Exit
```

Die Strategie ist ereignisgetrieben: Ein Whale-Buy wird unmittelbar in eine interne Queue geschrieben und dann bewertet. Preise, Trades und Orderbuchdaten stammen aus Binance-WebSockets. Im Orderpfad wird der aktuelle WebSocket-Preis verwendet; Binance-REST bleibt fuer die signierte Live-Order sowie beim ersten Auftreten eines Paars fuer die zwischengespeicherten Handelsregeln notwendig. Die tatsaechliche End-to-End-Latenz ist nicht garantiert.

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
    G1: Marktqualitaet?    Vol >= $5M/24h, Daten <= 30s, Spread <= 5bps,
                           Buchtiefe >= $25k, kein 24h-Absturz (< -5%)
        ↓ OK
    G2: Whale-Signal?      Frischer WHALE_BUY in letzten 30s
        ↓ OK
    G3: Buch-Persistenz?   >= 60% der Snapshots bid-dominant (min. 5 Snaps)
        ↓ OK
    G4: Flow + Momentum?   Kauf/Verkauf >= 1.6x UND 30s-Momentum +0.05%..+1.5%
        ↓ OK
    G5: Position frei?     Aktuelle Pos < MAX_POSITIONS
        ↓ OK
    ✅ MARKET BUY + Exit-Management
```

### G1 — Marktqualitaet
```
Bedingung: 24h-Volumen >= $5,000,000
           Letzte Datenaktualisierung <= 30 Sekunden
           Spread <= SCALP_MAX_SPREAD_BPS (Standard 5 bps)
           Bid-Tiefe >= SCALP_MIN_BID_DEPTH_USDT (Standard $25,000)
           24h-Change >= SCALP_REGIME_MAX_DROP_PCT (Standard -5%)
           erwartete Bewegung in der Haltedauer >= Take-Profit + Kosten
Warum: Enger Spread und echte Tiefe entscheiden beim Scalping ueber die Kosten.
       Der 24h-Wert dient nur noch als grober Crash-Schutz, nicht als Trendsignal.
       Stablecoin-Paare wie USDC/USDT werden ausgeschlossen, weil sie das Ziel
       nach Kosten nicht erreichen koennen.
```

### G2 — Whale Buy Signal (löst Event aus)
```
Bedingung: Einzelner aggTrade >= $50,000 USDT
           m == False (aggressiver Kauf)
           Signal < 30 Sekunden alt
```

### G3 — Orderbuch-Persistenz
```
Bedingung: >= SCALP_MIN_PERSISTENCE der Snapshots im Fenster bid-dominant
           mindestens SCALP_MIN_BOOK_SAMPLES Snapshots vorhanden
        Bid-Wand nicht mehr als SCALP_MAX_WALL_PULL_PCT geschrumpft
Warum: Ein einzelner Snapshot ist wertlos, weil Limit-Orders sofort verschwinden
       koennen. Nur anhaltende Bid-Dominanz zaehlt als Kaufdruck.
```

### Absorption und Wall-Pull
```
Absorption: Viele aggressive Verkaeufe treffen auf eine stabile Bid-Wand,
         aber der Preis faellt nicht. Das deutet auf einen grossen passiven
         Kaeufer hin und kann schwachen Kauf-Flow bei G4 bestaetigen.

Wall-Pull:  Die Bid-Tiefe faellt gegenueber ihrem Hoechstwert im Zeitfenster
         stark ab. Das deutet auf eine zurueckgezogene Kaufmauer bzw.
         Spoofing hin; der Bot lehnt den Kauf ab.
```

### G4 — Aggressiver Flow und Momentum
```
Bedingung: Kaufvolumen / Verkaufsvolumen >= SCALP_MIN_FLOW_RATIO (1.6x)
           oder bestaetigte Absorption
           Momentum im Fenster zwischen +0.05% und +1.5%
           mindestens 10 Trades im Fenster
Warum: Der Bot will echten Taker-Kaufdruck sehen und nicht einer Bewegung
       hinterherlaufen, die bereits ausgelaufen ist.
```

### G5 — Positions-Limit & Ausführung
```
Bedingung: len(offene_positionen) < MAX_POSITIONS (Standard: 10)
→ MARKET BUY platzieren
→ OCO SELL setzen (TP + SL gleichzeitig)
```

### Exit-Management
```
Take-Profit:    +BINANCE_TAKE_PROFIT_PCT
Stop-Loss:      -BINANCE_STOP_LOSS_PCT
Trailing:       ab +SCALP_TRAIL_ACTIVATE_PCT Gewinn, Ausstieg nach
                SCALP_TRAIL_GIVEBACK_PCT Rueckgabe vom Hoch
Zeit-Exit:      nach SCALP_MAX_HOLD_SEC (Standard 300s)
```

Die Ziel-Erreichbarkeit wird aus der echten Preisspanne im `SCALP_FLOW_WINDOW_SEC`-Fenster auf die maximale Haltedauer hochgerechnet. Das ist ein Kosten-/Volatilitaetsfilter, keine Kursprognose.

Die Paper-Simulation zieht Handelskosten ab:
`2 x BINANCE_TAKER_FEE_PCT + 2 x SCALP_SLIPPAGE_BPS`. Bei Standardwerten sind das rund `0.24%` pro Round-Trip. Liegt das Take-Profit-Ziel darunter, warnt der Bot beim Start, weil dann selbst Gewinntrades netto verlieren.

---

## 4. Kauf und Verkauf

### Kauf (Market Order)
```
Symbol:        XRPUSDT
Einstieg:      $2.4100 (Marktpreis zum Zeitpunkt des Signals)
Position:      $10 USDT (konfigurierbar)
Menge:         10 / 2.41 = 4.149 XRP (gerundet auf Lot-Size)
Ausfuehrung:   Marktpreis aus dem Live-WebSocket; Market-Order im Live-Modus,
               lokale Simulation im Paper-Modus
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

Im Paper-Modus wird kein Auftrag an Binance gesendet. Der Bot simuliert den Kauf und ueberwacht danach den Live-Preis. Bei Take-Profit, Stop-Loss, Trailing-Stop oder Zeitlimit wird ein simulierter Verkauf mit PnL in `binance_orderflow.db` gespeichert und die Position aus `positions.json` entfernt. Das Dashboard zeigt die verwendeten Gates, offene Paper-Positionen sowie simulierte Kaeufe und Verkaeufe.

## 5. Vor- und Nachteile

### Vorteile

- Oeffentliche Echtzeitdaten: Fuer Monitoring und Paper-Modus ist kein API-Key noetig.
- Der Bot begrenzt sich auf liquide USDT-Spot-Paare und eine konfigurierbare Zahl paralleler Positionen.
- Aggressiver Kauf, persistente Book-Imbalance, Wall-Pull-Schutz und kurzfristiger Flow muessen gemeinsam vorliegen.
- Absorption kann echten passiven Kaufdruck sichtbar machen, wenn aggressive Verkaeufer den Preis nicht druecken koennen.
- Der aktuelle Preis wird aus dem WebSocket genutzt; keine REST-Preisabfrage im Orderpfad.
- Eine erfolgreich angelegte OCO-Order kann im Live-Modus Gewinnziel und Verlustbegrenzung abbilden.
- Das Dashboard macht Verbindung, Signale, Kandidaten und Marktdaten sichtbar.

### Nachteile und Risiken

- Sichtbare Limit-Orders koennen vor der Ausfuehrung zurueckgezogen werden. Wall-Pull senkt dieses Risiko, beseitigt es aber nicht.
- Ein fester Whale-Schwellenwert ist fuer sehr liquide Paare weniger aussagekraeftig als fuer kleinere Paare.
- Market-Orders koennen Slippage und Gebuehren verursachen; die Paper-Simulation bildet beides nicht ab.
- Der 24h-Wert ist nur ein grober Crash-Filter und schuetzt nicht vor kurzfristigen Umkehrungen.
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

# ── Kurzfristiger Orderflow ───────────────────────────────
SCALP_FLOW_WINDOW_SEC=30        # Analysefenster fuer Flow und Momentum
SCALP_MIN_FLOW_RATIO=1.6        # Mindest-Kauf-/Verkaufsvolumen
SCALP_MIN_PERSISTENCE=0.6       # Anteil bid-dominanter Buch-Snapshots
SCALP_MIN_BOOK_SAMPLES=5        # Mindestzahl Buch-Snapshots
SCALP_MAX_WALL_PULL_PCT=40      # Maximales Schrumpfen der Bid-Wand
SCALP_MAX_SPREAD_BPS=5          # Maximaler Spread in Basispunkten
SCALP_MIN_BID_DEPTH_USDT=25000  # Mindesttiefe der besten 5 Bids
SCALP_MAX_HOLD_SEC=300          # Zeit-Exit nach 5 Minuten
SCALP_TRAIL_ACTIVATE_PCT=0.8    # Trailing-Stop ab diesem Gewinn
SCALP_TRAIL_GIVEBACK_PCT=0.4    # Maximaler Ruecklauf vom Hoch
BINANCE_TAKER_FEE_PCT=0.1       # Annahme je Seite fuer Paper-PnL
SCALP_SLIPPAGE_BPS=2            # Annahme je Seite fuer Paper-PnL
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
Binance Short-Term Order Flow Scalper
═══════════════════════════════════════════════════════════════
[ORDERFLOW] Warming up streams (10s)...
[ORDERFLOW] ✅ Mini-ticker connected (284 Paare)
[ORDERFLOW] ✅ Order flow streams active (20 Paare)
[ORDERFLOW] ⚡ Listening for whale trades...

── Status #1 | Tickers:284 | Pairs:20 | Signals:8 | Positions:0/10 ──

[ORDERFLOW] 🐋 BUY XRPUSDT | $186,746 @ $2.41
[XRPUSDT] G2✔ G3✔ G4✖ | Book bid-dominant 77% | Buy flow 1.36x < 1.60x

[ORDERFLOW] 🐋 BUY SOLUSDT | $95,200 @ $142.30
[SOLUSDT] ✅ ALL GATES | Price:$142.3000 | Whale BUY $95k | Book bid-dominant 80% | Flow 2.1x
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
