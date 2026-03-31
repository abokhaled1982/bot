import streamlit as st
import pandas as pd
import sqlite3
import requests
import json
import os
from datetime import datetime

st.set_page_config(page_title="Memecoin Bot", layout="wide", page_icon="🚀")

# ── Einstellungen ──────────────────────────────────────────────────────────────
POSITION_SIZE_USD = float(os.getenv("TRADE_MAX_POSITION_USD", "0.20"))
DB_PATH           = "memecoin_bot.db"
STOP_LOSS_PCT     = float(os.getenv("STOP_LOSS_PCT", "0.15"))
WALLET_ADDRESS    = os.getenv("SOLANA_WALLET_ADDRESS", "4jCowukxH9AR8Qxa3WseRiWcA1NzMMFprhgftat4yVBt")
SOLANA_RPC        = os.getenv("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")

# ── Helpers ────────────────────────────────────────────────────────────────────
@st.cache_data(ttl=30)
def get_live_price(address: str) -> float:
    try:
        r = requests.get(f"https://api.dexscreener.com/latest/dex/tokens/{address}", timeout=5)
        if r.status_code == 200:
            pairs = r.json().get("pairs", [])
            if pairs:
                return float(pairs[0].get("priceUsd") or 0)
    except Exception:
        pass
    return 0.0

@st.cache_data(ttl=30)
def get_token_info(address: str) -> dict:
    try:
        r = requests.get(f"https://api.dexscreener.com/latest/dex/tokens/{address}", timeout=5)
        if r.status_code == 200:
            pairs = r.json().get("pairs", [])
            if pairs:
                p = pairs[0]
                return {
                    "symbol":     p.get("baseToken", {}).get("symbol", "?"),
                    "price":      float(p.get("priceUsd") or 0),
                    "change_1h":  float(p.get("priceChange", {}).get("h1", 0) or 0),
                    "change_24h": float(p.get("priceChange", {}).get("h24", 0) or 0),
                    "volume_24h": float(p.get("volume", {}).get("h24", 0) or 0),
                    "liquidity":  float(p.get("liquidity", {}).get("usd", 0) or 0),
                    "dex_url":    p.get("url", ""),
                }
    except Exception:
        pass
    return {}

@st.cache_data(ttl=20)
def get_sol_price() -> float:
    try:
        r = requests.get(
            "https://api.coingecko.com/api/v3/simple/price?ids=solana&vs_currencies=usd",
            timeout=5
        )
        return float(r.json()["solana"]["usd"])
    except Exception:
        return 0.0

@st.cache_data(ttl=20)
def get_wallet_sol_balance(wallet: str) -> float:
    try:
        r = requests.post(SOLANA_RPC, json={
            "jsonrpc": "2.0", "id": 1,
            "method": "getBalance",
            "params": [wallet]
        }, timeout=8)
        return r.json()["result"]["value"] / 1e9
    except Exception:
        return 0.0

@st.cache_data(ttl=30)
def get_wallet_tokens(wallet: str) -> list:
    try:
        r = requests.post(SOLANA_RPC, json={
            "jsonrpc": "2.0", "id": 1,
            "method": "getTokenAccountsByOwner",
            "params": [
                wallet,
                {"programId": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"},
                {"encoding": "jsonParsed"}
            ]
        }, timeout=10)
        accounts = r.json().get("result", {}).get("value", [])
        tokens = []
        for acc in accounts:
            info   = acc["account"]["data"]["parsed"]["info"]
            mint   = info["mint"]
            amount = float(info["tokenAmount"]["uiAmount"] or 0)
            if amount > 0:
                tokens.append({"mint": mint, "amount": amount})
        return tokens
    except Exception:
        return []

@st.cache_data(ttl=60)
def get_wallet_transactions(wallet: str, limit: int = 20) -> list:
    try:
        # Signaturen holen
        r = requests.post(SOLANA_RPC, json={
            "jsonrpc": "2.0", "id": 1,
            "method": "getSignaturesForAddress",
            "params": [wallet, {"limit": limit}]
        }, timeout=10)
        sigs = r.json().get("result", [])
        txs  = []
        for sig in sigs:
            txs.append({
                "signature": sig.get("signature", ""),
                "time":      datetime.fromtimestamp(sig["blockTime"]).strftime("%d.%m.%Y %H:%M") if sig.get("blockTime") else "—",
                "status":    "✅ OK" if not sig.get("err") else "❌ Fehler",
                "slot":      sig.get("slot", ""),
            })
        return txs
    except Exception:
        return []

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

def color_pl(v):
    if isinstance(v, (int, float)):
        if v > 0: return "color: green; font-weight: bold"
        if v < 0: return "color: red;   font-weight: bold"
    return ""

def color_score(v):
    if isinstance(v, (int, float)):
        if v >= 70: return "color: green; font-weight: bold"
        if v >= 50: return "color: orange; font-weight: bold"
        return "color: red; font-weight: bold"
    return ""

# ── Sidebar ────────────────────────────────────────────────────────────────────
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
    sol_bal = get_wallet_sol_balance(WALLET_ADDRESS)
    sol_prc = get_sol_price()
    st.metric("💰 SOL Balance", f"{sol_bal:.4f} SOL", f"≈ ${sol_bal*sol_prc:.2f}")
    st.write(f"**Positionen:** {len(positions)} / 20")
    st.write(f"**Stop-Loss:** -{int(STOP_LOSS_PCT*100)}%")
    st.divider()
    if st.button("🔄 Aktualisieren", use_container_width=True):
        st.cache_data.clear()
        st.rerun()
    st.markdown(f"[🔗 Solscan Wallet](https://solscan.io/account/{WALLET_ADDRESS})")

# ── Titel + Tabs ───────────────────────────────────────────────────────────────
st.title("🚀 Memecoin Trading Bot")

tab1, tab2, tab3, tab4 = st.tabs([
    "📊 Trading Dashboard",
    "👛 Mein Wallet",
    "🛒 Kaufhistorie",
    "📜 Bot Log",
])


# ══════════════════════════════════════════════════════════════════════════════
# TAB 1 — TRADING DASHBOARD
# ══════════════════════════════════════════════════════════════════════════════
with tab1:
    # KPI
    df_kpi  = db_query("SELECT decision FROM trades")
    n_buy   = df_kpi["decision"].str.contains("BUY",  na=False).sum()
    n_sell  = df_kpi["decision"].str.contains("SELL", na=False).sum()

    total_pl = 0.0
    for addr, pos in positions.items():
        ep = float(pos.get("entry_price", 0))
        cp = get_live_price(addr)
        ps = float(pos.get("position_size", POSITION_SIZE_USD))
        if ep > 0 and cp > 0:
            total_pl += (ps / ep) * cp - ps

    k1, k2, k3, k4, k5 = st.columns(5)
    k1.metric("🟢 Offene Positionen", f"{len(positions)} / 20")
    k2.metric("📈 Käufe",             int(n_buy))
    k3.metric("📉 Verkäufe",          int(n_sell))
    k4.metric("💰 SOL Balance",       f"{sol_bal:.4f}", f"${sol_bal*sol_prc:.2f}")
    k5.metric("📊 Gesamt P/L",        f"${total_pl:+.4f}")

    st.divider()

    # Offene Positionen
    st.subheader("📍 Offene Positionen — Live")
    if positions:
        rows = []
        for addr, pos in positions.items():
            ep  = float(pos.get("entry_price") or 0)
            sym = pos.get("symbol", "—")
            ps  = float(pos.get("position_size", POSITION_SIZE_USD))
            cp  = get_live_price(addr)
            pl_pct = ((cp - ep) / ep * 100)           if ep > 0 and cp > 0 else 0.0
            pl_usd = (ps / ep) * cp - ps               if ep > 0 and cp > 0 else 0.0
            cur_val= (ps / ep) * cp                    if ep > 0 and cp > 0 else 0.0
            ts     = pos.get("timestamp", 0)
            secs   = datetime.now().timestamp() - ts   if ts else 0
            dur    = f"{int(secs//3600)}h {int((secs%3600)//60)}m" if secs > 0 else "—"
            tp1    = "✅" if pos.get("tp1_hit") else "⏳"
            tp2    = "✅" if pos.get("tp2_hit") else "⏳"
            rows.append({
                "Symbol":       sym,
                "Kaufpreis $":  f"{ep:.8f}",
                "Live Preis $": f"{cp:.8f}" if cp > 0 else "—",
                "Investiert $": ps,
                "Akt. Wert $":  round(cur_val, 4),
                "P/L %":        round(pl_pct, 2),
                "P/L USD":      round(pl_usd, 4),
                "TP1 +50%":     tp1,
                "TP2 +100%":    tp2,
                "Stop-Loss $":  f"{ep*(1-STOP_LOSS_PCT):.8f}",
                "Offen seit":   dur,
            })
        st.dataframe(
            pd.DataFrame(rows).style.map(color_pl, subset=["P/L %","P/L USD"]),
            use_container_width=True,
            height=min(80 + len(rows)*42, 500),
        )
    else:
        st.info("Keine offenen Positionen.")


# ══════════════════════════════════════════════════════════════════════════════
# TAB 2 — WALLET
# ══════════════════════════════════════════════════════════════════════════════
with tab2:
    st.subheader("👛 Mein Solana Wallet")
    st.caption(f"Adresse: `{WALLET_ADDRESS}`")
    st.markdown(f"🔗 [Auf Solscan ansehen](https://solscan.io/account/{WALLET_ADDRESS})")
    st.divider()

    # SOL Balance
    w1, w2, w3 = st.columns(3)
    w1.metric("SOL Balance",   f"{sol_bal:.6f} SOL")
    w2.metric("USD Wert",      f"${sol_bal * sol_prc:.4f}")
    w3.metric("SOL Preis",     f"${sol_prc:.2f}")

    st.divider()

    # Token Bestände
    st.subheader("🪙 Token Bestände")
    st.caption("Alle Token die gerade in deinem Wallet sind")

    with st.spinner("Lade Token..."):
        tokens = get_wallet_tokens(WALLET_ADDRESS)

    if tokens:
        token_rows = []
        for t in tokens:
            mint   = t["mint"]
            amount = t["amount"]
            info   = get_token_info(mint)
            price  = info.get("price", 0)
            sym    = info.get("symbol", mint[:8]+"...")
            wert   = amount * price
            ch_1h  = info.get("change_1h", 0)
            ch_24h = info.get("change_24h", 0)
            dex_url= info.get("dex_url", "")
            token_rows.append({
                "Symbol":       sym,
                "Menge":        f"{amount:,.2f}",
                "Preis $":      f"{price:.10f}" if price > 0 else "—",
                "Wert $":       round(wert, 4),
                "1h %":         round(ch_1h, 2),
                "24h %":        round(ch_24h, 2),
                "Adresse":      mint[:20]+"...",
                "DexScreener":  dex_url,
            })

        df_tok = pd.DataFrame(token_rows)
        st.dataframe(
            df_tok.drop(columns=["DexScreener"]).style
                .map(color_pl, subset=["1h %","24h %"]),
            use_container_width=True,
            height=min(80 + len(token_rows)*42, 400),
        )

        # Links zu DexScreener
        st.markdown("**🔗 Direkt auf DexScreener:**")
        for t in token_rows:
            if t["DexScreener"]:
                st.markdown(f"- [{t['Symbol']}]({t['DexScreener']})")
    else:
        st.info("Keine Token im Wallet gefunden.")

    st.divider()

    # Transaktions-Historie wie Solscan
    st.subheader("📋 Transaktions-Historie")
    st.caption("Letzte Transaktionen — wie auf Solscan")

    n_tx = st.selectbox("Anzahl Transaktionen", [10, 20, 50], index=1)

    with st.spinner("Lade Transaktionen von Solana..."):
        txs = get_wallet_transactions(WALLET_ADDRESS, limit=n_tx)

    if txs:
        df_tx = pd.DataFrame(txs)
        df_tx["Solscan Link"] = df_tx["signature"].apply(
            lambda s: f"https://solscan.io/tx/{s}"
        )
        df_tx["TX"] = df_tx["signature"].str[:20] + "..."

        def color_status(v):
            if "OK"     in str(v): return "color: green; font-weight: bold"
            if "Fehler" in str(v): return "color: red;   font-weight: bold"
            return ""

        # Tabelle ohne rohe Signature
        df_show = df_tx[["time","TX","status","slot"]].rename(columns={
            "time":   "Zeitpunkt",
            "TX":     "Transaktion",
            "status": "Status",
            "slot":   "Block",
        })

        st.dataframe(
            df_show.style.map(color_status, subset=["Status"]),
            use_container_width=True,
            height=min(80 + len(df_show)*42, 600),
        )

        # Links
        st.markdown("**🔗 Transaktionen auf Solscan:**")
        for _, tx in df_tx.iterrows():
            icon = "✅" if "OK" in tx["status"] else "❌"
            st.markdown(f"{icon} [{tx['TX']}](https://solscan.io/tx/{tx['signature']}) — {tx['time']}")
    else:
        st.info("Keine Transaktionen gefunden.")


# ══════════════════════════════════════════════════════════════════════════════
# TAB 3 — KAUFHISTORIE
# ══════════════════════════════════════════════════════════════════════════════
with tab3:
    st.subheader("🛒 Kaufhistorie — Alle Trades")
    st.caption("Nur echte Käufe mit Preis, P/L und Kaufgrund")

    f1, f2, f3 = st.columns([2,1,1])
    with f1:
        suche = st.text_input("🔍 Symbol suchen", placeholder="z.B. NoKings ...")
    with f2:
        pl_filter = st.selectbox("Filter", ["Alle","Nur Gewinn 🟢","Nur Verlust 🔴"])
    with f3:
        anzahl = st.selectbox("Max. Einträge", [25,50,100], index=1)

    df_raw = db_query(f"""
        SELECT symbol, token_address, entry_price, position_size,
               score, decision, ai_reasoning, funnel_stage, timestamp
        FROM   trades
        WHERE  decision IN ('BUY','SELL','TP1','TP2','TP3','STOP_LOSS')
           OR  decision LIKE '%BUY%'
        ORDER  BY timestamp DESC
        LIMIT  {anzahl}
    """)

    if not df_raw.empty:
        if suche:
            df_raw = df_raw[df_raw["symbol"].str.contains(suche, case=False, na=False)]

        rows = []
        for _, row in df_raw.iterrows():
            sym   = str(row["symbol"] or "—")
            addr  = str(row["token_address"])
            ep    = float(row["entry_price"] or 0)
            ps    = float(row["position_size"] or POSITION_SIZE_USD)
            score = float(row["score"] or 0)
            dec   = str(row["decision"])
            ts    = str(row["timestamp"])[:16]

            # Kaufgrund
            grund = "—"
            try:
                ai    = json.loads(row["ai_reasoning"] or "{}")
                sent  = ai.get("sentiment","")
                flags = ai.get("risk_flags", [])
                sigs  = ai.get("key_signals", [])
                parts = []
                if sent:   parts.append(sent)
                if flags:  parts.append(", ".join(flags[:2]))
                if sigs:   parts.append(sigs[0] if sigs else "")
                grund = " | ".join(p for p in parts if p) or "—"
            except Exception:
                pass

            cp   = get_live_price(addr)
            pct  = (cp - ep) / ep * 100  if ep > 0 and cp > 0 else 0.0
            pusd = (ps / ep) * cp - ps   if ep > 0 and cp > 0 else 0.0
            res  = "🟢 GEWINN" if pct > 0 else ("🔴 VERLUST" if pct < 0 else "⚪ N/A")

            rows.append({
                "Zeitpunkt":    ts,
                "Symbol":       sym,
                "Typ":          dec,
                "Kaufpreis $":  f"{ep:.8f}" if ep > 0 else "—",
                "Live Preis $": f"{cp:.8f}" if cp > 0 else "—",
                "Investiert $": ps,
                "P/L %":        round(pct, 2),
                "P/L USD":      round(pusd, 4),
                "Ergebnis":     res,
                "Score":        round(score, 1),
                "Kaufgrund":    grund,
                "Adresse":      addr[:18]+"...",
            })

        df_show = pd.DataFrame(rows)
        if pl_filter == "Nur Gewinn 🟢":  df_show = df_show[df_show["P/L %"] > 0]
        if pl_filter == "Nur Verlust 🔴": df_show = df_show[df_show["P/L %"] < 0]

        def hl_res(v):
            if "GEWINN"  in str(v): return "color: green; font-weight: bold"
            if "VERLUST" in str(v): return "color: red;   font-weight: bold"
            return "color: gray"

        st.dataframe(
            df_show.style
                .map(hl_res,     subset=["Ergebnis"])
                .map(color_pl,   subset=["P/L %","P/L USD"])
                .map(color_score,subset=["Score"]),
            use_container_width=True,
            height=min(80 + len(df_show)*42, 650),
        )

        # Zusammenfassung
        st.divider()
        t_win  = (df_show["P/L %"] > 0).sum()
        t_lose = (df_show["P/L %"] < 0).sum()
        t_pl   = df_show["P/L USD"].sum()
        wr     = round(t_win / len(df_show) * 100, 1) if len(df_show) > 0 else 0

        s1,s2,s3,s4,s5 = st.columns(5)
        s1.metric("📊 Trades",      len(df_show))
        s2.metric("🟢 Gewinne",     t_win)
        s3.metric("🔴 Verluste",    t_lose)
        s4.metric("🎯 Win-Rate",    f"{wr}%")
        s5.metric("💰 Gesamt P/L",  f"${t_pl:+.4f}")
    else:
        st.info("Noch keine Trades in der Datenbank.")


# ══════════════════════════════════════════════════════════════════════════════
# TAB 4 — BOT LOG
# ══════════════════════════════════════════════════════════════════════════════
with tab4:
    st.subheader("📜 Live Bot Log")
    st.caption("Was der Bot gerade analysiert und entscheidet")

    log_filter = st.selectbox("Filter", ["Alle","ERROR","WARNING","INFO"])
    n_logs     = st.selectbox("Anzahl", [30, 50, 100], index=0)

    sql = f"""
        SELECT level AS Level, message AS Nachricht, timestamp AS Zeit
        FROM   bot_logs
        {"WHERE level = '" + log_filter + "'" if log_filter != 'Alle' else ''}
        ORDER  BY timestamp DESC
        LIMIT  {n_logs}
    """
    df_log = db_query(sql)

    if not df_log.empty:
        def cl(v):
            if v == "ERROR":   return "color: red;    font-weight: bold"
            if v == "WARNING": return "color: orange; font-weight: bold"
            if v == "SUCCESS": return "color: green;  font-weight: bold"
            return ""
        st.dataframe(
            df_log.style.map(cl, subset=["Level"]),
            use_container_width=True,
            height=500,
        )
    else:
        st.info("Keine Logs.")
