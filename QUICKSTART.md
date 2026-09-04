# Quickstart – Trader finden, kopieren, überwachen

Diese Anleitung erklärt in Kurzform die drei CLI-Skripte, mit denen du Trader
findest, simuliert kopierst und live mitverfolgst. Alle drei laufen ohne
Binance-API-Key – sie nutzen ausschliesslich die öffentlichen Endpunkte des
Binance-Copy-Trading-Leaderboards.

Voraussetzung: aktiviertes venv und installierte Requirements.

```bash
source .venv/bin/activate
pip install -r requirements.txt   # nur beim ersten Mal
```

---

## 1. `find_traders.py` – Trader suchen

Sucht auf dem Binance-Futures-Leaderboard nach Tradern, die die angegebenen
Filter (Winrate, Tages-ROI, Tages-PnL) erfüllen, und listet sie auf. Optional
werden neue Kandidaten direkt in `traders_export.json` **ergänzt** (ohne
Duplikate, ohne bestehende Einträge zu überschreiben).

**Nur anzeigen (Top-20 nach Standardkriterien):**
```bash
python3 find_traders.py
```

**Nur Winrate ≥ 80 %, PnL/ROI-Filter ausschalten, Ergebnis in
`traders_export.json` ergänzen:**
```bash
python3 find_traders.py \
  --min-win-rate 80 --min-day-roi 0 --min-day-pnl 0 \
  --pool-size 2000 --limit 500 \
  --append-to traders_export.json
```

Wichtige Optionen:

| Flag | Bedeutung |
|------|-----------|
| `--min-win-rate 80` | Nur Trader mit Winrate ≥ 80 % |
| `--min-day-roi 0` / `--min-day-pnl 0` | Standard-Profitfilter deaktivieren |
| `--pool-size 2000` | Grösse des Kandidaten-Pools beim Scan |
| `--limit 500` | Max. Anzahl zurückgelieferter Trader |
| `--append-to traders_export.json` | Neue Trader in JSON schreiben (Dedup per `wallet`) |
| `--activate` | Neu ergänzte Trader direkt mit `is_copied=1` |
| `--all` | Auch nicht-verifizierte Kandidaten anzeigen |

Nach dem Lauf meldet das Skript, wie viele Einträge neu waren und wie viele
schon in der JSON standen.

---

## 2. `simulate_copytrader.py` – Trader simuliert kopieren

Standalone-Simulator ohne Dashboard. Liest `traders_export.json` bei **jedem**
Poll neu ein, spiegelt LONG-Positionen der markierten Trader als Paper-Trades
zum aktuellen Binance-Spot-Preis wider und protokolliert PnL in USDT und EUR.

**Auswahl-Regel:** aktiv ⇔ `is_copied == 1` **und** `win_rate ≥ --min-win-rate`.
Wird ein Trader entfernt oder auf `is_copied=0` gesetzt, schliesst der
Simulator seine offenen Sim-Positionen automatisch beim nächsten Poll.

**Standard-Start (10 USDT pro Trade, Winrate-Schwelle 80 %, Poll alle 5 s):**
```bash
python3 simulate_copytrader.py
```

**Mit eigenen Parametern:**
```bash
python3 simulate_copytrader.py \
  --min-win-rate 80 --size-usdt 10 \
  --poll-interval 5 --usdt-eur-rate 0.92
```

Wichtige Optionen:

| Flag | Bedeutung |
|------|-----------|
| `--min-win-rate 80` | Winrate-Schwelle für aktive Trader |
| `--size-usdt 10` | Fixe Positionsgrösse pro simuliertem Trade |
| `--poll-interval 5` | Sekunden zwischen zwei Iterationen |
| `--usdt-eur-rate 0.92` | Kurs für PnL-Anzeige in EUR |
| `--traders-file …` | Alternative Trader-Liste (Default `traders_export.json`) |

Ausgabedateien (werden im Repo-Root live fortgeschrieben):

- `sim_positions_open.json` – aktuell **offene** Paper-Positionen
  (Trader-ID, URL, Coin, Entry-Preis, Grösse, Zeitstempel)
- `sim_positions_closed.json` – **geschlossene** Paper-Trades (Historie mit
  `pnl_usdt`, `pnl_eur`, `close_reason` = `TRADER_CLOSED` oder
  `TRADER_UNTRACKED`, Zeitstempel offen/geschlossen)
- `sim_trader_stats.json` – **Status pro Trader**: Trades, Winrate,
  Gesamt-PnL USDT+EUR, `verdict` = `HELPFUL` / `HURTFUL` / `NEUTRAL`,
  sortiert nach PnL

> Ältere `sim_positions.json` / `sim_history.json` werden beim ersten Start
> automatisch in die neuen Dateien übernommen.

Beenden mit `Ctrl+C`. Offene Positionen bleiben in `sim_positions_open.json`
erhalten und werden beim nächsten Start weitergeführt.

---

## 3. `monitor_traders.py` – Live-Signale mitverfolgen

Zeigt in Echtzeit, was die auf dem Leaderboard gefundenen Trader tatsächlich
tun – öffnen/schliessen/erhöhen von Positionen – ohne Simulation und ohne
echte Orders. Nützlich, um einen Trader vor dem Aktivieren „live"
anzuschauen.

**Top-5 Intraday-Trader beobachten:**
```bash
python3 monitor_traders.py
```

**Trader mit Winrate ≥ 80 % beobachten und Paper-Copy-Meldungen anzeigen:**
```bash
python3 monitor_traders.py \
  --min-win-rate 80 --min-day-roi 0 --min-day-pnl 0 \
  --limit 10 --paper-copy
```

Wichtige Optionen:

| Flag | Bedeutung |
|------|-----------|
| `--limit 10` | Wieviele Trader gleichzeitig überwachen |
| `--pool-size 50` | Kandidaten-Pool für den Vorab-Scan |
| `--poll-interval 5` | Poll-Takt |
| `--min-win-rate 80` | Winrate-Filter |
| `--paper-copy` | Ausgabe der Paper-Copy-Entscheidungen der Pipeline |
| `--show-status` | Periodisch Trader-Status/Telemetrie mitloggen |
| `--all` | Auch nicht-verifizierte Kandidaten einbeziehen |

Beenden mit `Ctrl+C`.

---

## Typischer Workflow

1. **Trader suchen und in Liste aufnehmen**
   ```bash
   python3 find_traders.py --min-win-rate 80 --min-day-roi 0 --min-day-pnl 0 \
     --pool-size 2000 --limit 500 --append-to traders_export.json
   ```
2. **`traders_export.json` öffnen und interessante Trader manuell auf
   `"is_copied": 1` setzen.** Uninteressante Einträge können gelöscht werden.
3. **Simulator starten und laufen lassen:**
   ```bash
   python3 simulate_copytrader.py
   ```
4. **Optional parallel `monitor_traders.py`** starten, um Live-Aktionen mit den
   Sim-Trades in `sim_history.json` zu vergleichen.
5. Nach ein paar Tagen `sim_trader_stats.json` prüfen – Trader mit
   `verdict = HELPFUL` und positivem `pnl_eur` sind Kandidaten zum echten
   Kopieren.
