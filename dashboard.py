import streamlit as st
import pandas as pd
import sqlite3
import requests
import json
import os
from datetime import datetime

st.set_page_config(page_title="Memecoin Bot", layout="wide", page_icon="🚀")

# ─── Einstellungen ────────────────────────────────────────────────────────────
POSITION_SIZE_USD = float(os.getenv("TRADE_MAX_POSITION_USD", "0.20"))
DB_PATH           = "memecoin_bot.db"
STOP_LOSS_PCT     = float(os.getenv("STOP_LOSS_PCT", "0.15"))

# ─── Helpers ──────────────────────────────────────────────────────────────────
@st.cache_data(ttl=30)
def get_live_price(address: str) -> float:
    try:
        r = requests.get(
            f"https://api.dexscreener.com/latest/dex/tokens/{address}",
            timeout=5
        )
        if r.status_code == 200:
            pairs = r.json().get("pairs", [])
            if pairs:
                return float(pairs[0].get("priceUsd") or 0)
    except Exception:
        pass
    return 0.0

def db_query(sql: str) -> pd.DataFrame:
    conn = sqlite3.connect(DB_PATH)
    df   = pd.read_sql_query(sql, conn)
    conn.close()
    return df

def load_positions() -> dict:
    if os.path.exists("positions.json"):
        try:
            with open("positions.json") as f:
                return json.load(f)
        except Exception:
            pass
    return {}

# ─── Sidebar ──────────────────────────────────────────────────────────────────
positions = load_positions()

with st.sidebar:
    st.markdown("## 🚀 Memecoin Bot")
    st.caption(datetime.now().strftime("%d.%m.%Y  %H:%M:%S"))
    st.divider()

    if os.path.exists("STOP_BOT"):
        st.error("🔴 Bot GESTOPPT")
        if st.button("▶️ Bot starten", use_container_width=True):
            os.remove("STOP_BOT")
            st.rerun()
    else:
        st.success("🟢 Bot läuft")
        if st.button("🛑 STOP", use_container_width=True, type="primary"):
            open("STOP_BOT", "w").write("STOP")
            st.rerun()

    st.divider()
    st.write(f"**Offene Positionen:** {len(positions)} / 20")
    st.write(f"**Position Größe:** ${POSITION_SIZE_USD}")
    st.write(f"**Stop-Loss:** {int(STOP_LOSS_PCT*100)}%")
    st.divider()

    if st.button("🔄 Aktualisieren", use_container_width=True):
        st.cache_data.clear()
        st.rerun()

# ─── Titel ────────────────────────────────────────────────────────────────────
st.title("🚀 Memecoin Trading Bot")
st.divider()

# ─── KPI ──────────────────────────────────────────────────────────────────────
df_kpi = db_query("SELECT decision FROM trades")
n_buy  = df_kpi["decision"].str.contains("BUY",  na=False).sum()
n_sell = df_kpi["decision"].str.contains("SELL", na=False).sum()
n_hold = df_kpi["decision"].isin(["HOLD","SKIP"]).sum()

c1, c2, c3, c4 = st.columns(4)
c1.metric("🟢 Offene Positionen", f"{len(positions)} / 20")
c2.metric("📈 Käufe gesamt",      int(n_buy))
c3.metric("📉 Verkäufe gesamt",   int(n_sell))
c4.metric("⏸️ Übersprungen",      int(n_hold))

st.divider()

# ══════════════════════════════════════════════════════════════════════════════
#  TABELLE 1 — LIVE POSITION TRACKER (aktuell offen)
# ══════════════════════════════════════════════════════════════════════════════
st.subheader("📍 Offene Positionen — Live Tracker")
st.caption("Nur echte / simulierte Käufe die gerade offen sind")

if positions:
    rows = []
    for addr, pos in positions.items():
        ep  = float(pos.get("entry_price") or 0)
        sym = pos.get("symbol", "—")
        cp  = get_live_price(addr)

        pl_pct = ((cp - ep) / ep * 100) if ep > 0 and cp > 0 else 0.0
        pl_usd = ((POSITION_SIZE_USD / ep) * cp - POSITION_SIZE_USD) if ep > 0 and cp > 0 else 0.0

        ts   = pos.get("timestamp", 0)
        secs = datetime.now().timestamp() - ts if ts else 0
        dur  = f"{int(secs//3600)}h {int((secs%3600)//60)}m" if secs > 0 else "—"

        rows.append({
            "Symbol":        sym,
            "Kaufpreis $":   f"{ep:.8f}" if ep > 0 else "—",
            "Live Preis $":  f"{cp:.8f}" if cp > 0 else "kein Preis",
            "P/L %":         round(pl_pct, 2),
            "P/L USD":       round(pl_usd, 4),
            "Investiert $":  POSITION_SIZE_USD,
            "Akt. Wert $":   round((POSITION_SIZE_USD / ep) * cp, 4) if ep > 0 and cp > 0 else "—",
            "Offen seit":    dur,
            "Stop-Loss $":   f"{ep * (1 - STOP_LOSS_PCT):.8f}" if ep > 0 else "—",
            "Adresse":       addr[:20] + "...",
        })

    df_live = pd.DataFrame(rows)

    def color_pl(v):
        if isinstance(v, (int, float)):
            if v > 0:  return "color: green; font-weight: bold"
            if v < 0:  return "color: red;   font-weight: bold"
        return ""

    st.dataframe(
        df_live.style.map(color_pl, subset=["P/L %", "P/L USD"]),
        use_container_width=True,
        height=min(80 + len(rows) * 40, 500),
    )
else:
    st.info("Keine offenen Positionen.")

st.divider()

# ══════════════════════════════════════════════════════════════════════════════
#  TABELLE 2 — GEKAUFTE COINS (die wichtigste Tabelle)
# ══════════════════════════════════════════════════════════════════════════════
st.subheader("🛒 Gekaufte Coins — Kaufhistorie")
st.caption("Jeder Kauf den der Bot gemacht hat — mit Zeitstempel, Preis, Gewinn/Verlust und Kaufgrund")

# Filter
f1, f2, f3 = st.columns([2, 1, 1])
with f1:
    suche = st.text_input("🔍 Symbol suchen", placeholder="z.B. MSTR, PURGE ...")
with f2:
    pl_filter = st.selectbox("P/L Filter", ["Alle", "Nur Gewinn 🟢", "Nur Verlust 🔴"])
with f3:
    anzahl = st.selectbox("Einträge", [25, 50, 100, 200], index=1)

# Nur echte BUY Einträge mit echtem Preis holen
df_raw = db_query(f"""
    SELECT
        symbol,
        token_address,
        entry_price,
        position_size,
        score,
        decision,
        ai_reasoning,
        timestamp
    FROM trades
    WHERE decision LIKE '%BUY%'
    ORDER BY timestamp DESC
    LIMIT {anzahl}
""")

if not df_raw.empty:
    if suche:
        df_raw = df_raw[df_raw["symbol"].str.contains(suche, case=False, na=False)]

    result_rows = []
    for _, row in df_raw.iterrows():
        sym  = str(row["symbol"] or "—")
        addr = str(row["token_address"])
        ep   = float(row["entry_price"] or 0)
        ps   = float(row["position_size"] or POSITION_SIZE_USD)
        ts   = str(row["timestamp"])
        dec  = str(row["decision"])
        score= float(row["score"] or 0)

        # Kaufgrund aus ai_reasoning extrahieren
        grund = "Score ≥ 60 + Safety OK"
        try:
            ai = json.loads(row["ai_reasoning"] or "{}")
            flags = ai.get("risk_flags", [])
            sent  = ai.get("sentiment", "")
            if flags or sent:
                grund = f"{sent} | {', '.join(flags[:2])}" if sent else ", ".join(flags[:2])
        except Exception:
            pass

        # Modus anzeigen
        modus = "🔴 LIVE" if "SIMULATED" not in dec and "DRY" not in dec else "🟡 SIMULATION"

        # Live Preis holen
        cp = get_live_price(addr)

        if ep > 0 and cp > 0:
            pct  = (cp - ep) / ep * 100
            pusd = (ps / ep) * cp - ps
            res  = "🟢 GEWINN" if pct > 0 else "🔴 VERLUST"
        else:
            pct = pusd = 0.0
            res = "⚪ N/A"

        result_rows.append({
            "Zeitpunkt":     ts[:16],
            "Symbol":        sym,
            "Modus":         modus,
            "Kaufpreis $":   f"{ep:.8f}" if ep > 0 else "0 (Bug)",
            "Live Preis $":  f"{cp:.8f}" if cp > 0 else "—",
            "Investiert $":  ps,
            "P/L %":         round(pct, 2),
            "P/L USD":       round(pusd, 4),
            "Ergebnis":      res,
            "Score":         round(score, 1),
            "Kaufgrund":     grund,
            "Adresse":       addr[:20] + "...",
        })

    df_show = pd.DataFrame(result_rows)

    # P/L Filter anwenden
    if pl_filter == "Nur Gewinn 🟢":
        df_show = df_show[df_show["P/L %"] > 0]
    elif pl_filter == "Nur Verlust 🔴":
        df_show = df_show[df_show["P/L %"] < 0]

    def color_res(v):
        s = str(v)
        if "GEWINN"  in s: return "color: green; font-weight: bold"
        if "VERLUST" in s: return "color: red;   font-weight: bold"
        return "color: gray"

    def color_pl(v):
        if isinstance(v, (int, float)):
            if v > 0: return "color: green; font-weight: bold"
            if v < 0: return "color: red;   font-weight: bold"
        return ""

    def color_score(v):
        if isinstance(v, (int, float)):
            if v >= 70: return "color: green; font-weight: bold"
            if v >= 50: return "color: orange"
            return "color: red"
        return ""

    st.dataframe(
        df_show.style
            .map(color_res,   subset=["Ergebnis"])
            .map(color_pl,    subset=["P/L %", "P/L USD"])
            .map(color_score, subset=["Score"]),
        use_container_width=True,
        height=min(80 + len(df_show) * 40, 650),
    )

    # Zusammenfassung
    st.markdown("---")
    t_win  = (df_show["P/L %"] > 0).sum()
    t_lose = (df_show["P/L %"] < 0).sum()
    t_pl   = df_show["P/L USD"].sum()
    wr     = round(t_win / len(df_show) * 100, 1) if len(df_show) > 0 else 0

    x1, x2, x3, x4, x5 = st.columns(5)
    x1.metric("📊 Einträge",      len(df_show))
    x2.metric("🟢 Gewinne",       t_win)
    x3.metric("🔴 Verluste",      t_lose)
    x4.metric("🎯 Win-Rate",      f"{wr}%")
    x5.metric("💰 Gesamt P/L",    f"${t_pl:+.4f}")

    # Hinweis wenn entry_price = 0
    n_zero = (df_show["Kaufpreis $"] == "0 (Bug)").sum()
    if n_zero > 0:
        st.warning(
            f"⚠️ {n_zero} Einträge haben Kaufpreis = 0. Das war ein alter Bug (ist jetzt gefixt). "
            f"Diese Einträge stammen aus alten Läufen bevor der Fix aktiv war. "
            f"P/L kann für diese Einträge nicht berechnet werden."
        )
else:
    st.info("Noch keine Käufe in der Datenbank.")

st.divider()

# ══════════════════════════════════════════════════════════════════════════════
#  TABELLE 3 — EVENT LOG
# ══════════════════════════════════════════════════════════════════════════════
st.subheader("📜 Bot Log")
st.caption("Was der Bot gerade macht")

df_log = db_query("""
    SELECT level AS "Level", message AS "Nachricht", timestamp AS "Zeit"
    FROM   bot_logs
    ORDER  BY timestamp DESC
    LIMIT  30
""")

if not df_log.empty:
    def color_level(v):
        if v == "ERROR":   return "color: red;    font-weight: bold"
        if v == "WARNING": return "color: orange; font-weight: bold"
        if v == "SUCCESS": return "color: green;  font-weight: bold"
        return ""
    st.dataframe(
        df_log.style.map(color_level, subset=["Level"]),
        use_container_width=True,
        height=280,
    )
else:
    st.info("Keine Logs.")
