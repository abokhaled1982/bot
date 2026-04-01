import streamlit as st
import pandas as pd
import sqlite3
import requests
import json
import os
import time
import plotly.graph_objects as go
import plotly.express as px
from datetime import datetime

# ══════════════════════════════════════════════════════════════════════════════
# CONFIG
# ══════════════════════════════════════════════════════════════════════════════
st.set_page_config(
    page_title="Memecoin Terminal",
    layout="wide",
    page_icon="logo.png" if os.path.exists("logo.png") else None,
    initial_sidebar_state="expanded",
)

POSITION_SIZE_USD = float(os.getenv("TRADE_MAX_POSITION_USD", "1.0"))
DB_PATH           = "memecoin_bot.db"
STOP_LOSS_PCT     = float(os.getenv("STOP_LOSS_PCT", "0.20"))
TRAILING_STOP_PCT = float(os.getenv("TRAILING_STOP_PCT", "0.25"))
TRAILING_ACTIVATE = float(os.getenv("TRAILING_ACTIVATE", "0.30"))
TP1_PCT           = float(os.getenv("TP1_PCT", "0.50"))
TP2_PCT           = float(os.getenv("TP2_PCT", "1.00"))
TP3_PCT           = float(os.getenv("TP3_PCT", "2.00"))
MAX_HOLD_HOURS    = float(os.getenv("MAX_HOLD_HOURS", "24"))
WALLET_ADDRESS    = os.getenv(
    "SOLANA_WALLET_ADDRESS",
    "4jCowukxH9AR8Qxa3WseRiWcA1NzMMFprhgftat4yVBt",
)


# ══════════════════════════════════════════════════════════════════════════════
# CUSTOM CSS — dark trading terminal theme
# ══════════════════════════════════════════════════════════════════════════════
st.markdown("""
<style>
    /* ── Base: solid dark backgrounds ────────────────────────────────────── */
    .stApp { background-color: #1a1d23; color: #d1d5db; }
    section[data-testid="stSidebar"] { background-color: #14161b; border-right: 1px solid #2a2d35; }

    /* ── Tabs ────────────────────────────────────────────────────────────── */
    .stTabs [data-baseweb="tab-list"] { gap: 0; border-bottom: 1px solid #2a2d35; }
    .stTabs [data-baseweb="tab"] {
        background: none; border-radius: 0; border: none;
        padding: 10px 22px; color: #6b7280; font-weight: 500; font-size: 0.88rem;
    }
    .stTabs [data-baseweb="tab"]:hover { color: #d1d5db; }
    .stTabs [aria-selected="true"] {
        background: none !important; color: #60a5fa !important;
        border-bottom: 2px solid #60a5fa !important;
    }

    /* ── KPI cards ───────────────────────────────────────────────────────── */
    .kpi-card {
        background: #22252b; border: 1px solid #2a2d35; border-radius: 10px;
        padding: 16px 18px; text-align: center;
    }
    .kpi-card .label { color: #9ca3af; font-size: 0.72rem; margin-bottom: 4px; text-transform: uppercase; letter-spacing: 0.8px; }
    .kpi-card .value { color: #f3f4f6; font-size: 1.4rem; font-weight: 700; }
    .kpi-card .sub   { color: #6b7280; font-size: 0.72rem; margin-top: 3px; }

    /* ── Position cards ──────────────────────────────────────────────────── */
    .pos-card {
        background: #22252b; border: 1px solid #2a2d35; border-radius: 10px;
        padding: 16px; margin-bottom: 8px;
    }
    .pos-card:hover { border-color: #3b82f6; }
    .pos-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 10px; }
    .pos-symbol { font-size: 1.15rem; font-weight: 700; color: #f3f4f6; }
    .pos-badge  { padding: 3px 10px; border-radius: 5px; font-size: 0.72rem; font-weight: 600; }
    .badge-profit { background: #052e16; color: #4ade80; }
    .badge-loss   { background: #450a0a; color: #f87171; }
    .badge-neutral{ background: #422006; color: #fbbf24; }
    .pos-grid   { display: grid; grid-template-columns: repeat(4, 1fr); gap: 8px; }
    .pos-item .lbl { color: #6b7280; font-size: 0.7rem; text-transform: uppercase; }
    .pos-item .val { color: #e5e7eb; font-size: 0.9rem; font-weight: 600; }

    /* ── P/L colors ──────────────────────────────────────────────────────── */
    .profit { color: #4ade80 !important; }
    .loss   { color: #f87171 !important; }

    /* ── Log entries ─────────────────────────────────────────────────────── */
    .log-error   { color: #f87171; }
    .log-warning { color: #fbbf24; }
    .log-success { color: #4ade80; }
    .log-info    { color: #9ca3af; }

    /* ── Section headers ─────────────────────────────────────────────────── */
    .section-header {
        color: #f3f4f6; font-size: 1.05rem; font-weight: 600;
        padding-bottom: 8px; border-bottom: 1px solid #2a2d35;
        margin-bottom: 14px;
    }

    /* ── Status indicator ────────────────────────────────────────────────── */
    .status-dot { display: inline-block; width: 10px; height: 10px; border-radius: 50%; margin-right: 6px; }
    .status-running { background: #4ade80; box-shadow: 0 0 6px #4ade80; }
    .status-stopped { background: #f87171; box-shadow: 0 0 6px #f87171; }

    /* ── History card ────────────────────────────────────────────────────── */
    .hist-card {
        background: #22252b; border: 1px solid #2a2d35;
        border-radius: 8px; padding: 12px 16px; margin-bottom: 6px;
    }
    .hist-card:hover { border-color: #3b82f6; }

    /* ── Gate pills ──────────────────────────────────────────────────────── */
    .gate-pass { background: #052e16; color: #4ade80; padding: 2px 6px; border-radius: 3px; font-size: 0.68rem; margin-right: 2px; font-weight: 600; display: inline-block; }
    .gate-fail { background: #1f2028; color: #4b5563; padding: 2px 6px; border-radius: 3px; font-size: 0.68rem; margin-right: 2px; font-weight: 500; display: inline-block; }

    /* ── Insight box ─────────────────────────────────────────────────────── */
    .insight-box {
        background: #1e2736; border: 1px solid #2a3a52; border-left: 3px solid #3b82f6;
        border-radius: 6px; padding: 12px 16px; margin-top: 8px;
    }
    .insight-box .title { color: #60a5fa; font-size: 0.75rem; font-weight: 600; margin-bottom: 6px; text-transform: uppercase; letter-spacing: 0.5px; }
    .insight-box .text  { color: #d1d5db; font-size: 0.82rem; line-height: 1.6; }

    /* ── Info badge ──────────────────────────────────────────────────────── */
    .info-badge {
        display: inline-block; background: #1f2028; color: #9ca3af;
        padding: 2px 8px; border-radius: 4px; font-size: 0.7rem; margin-right: 4px;
    }

    /* ── Hide default streamlit ──────────────────────────────────────────── */
    #MainMenu {visibility: hidden;}
    footer {visibility: hidden;}
    .stDeployButton {display: none;}
    div[data-testid="stMetric"] {
        background: #22252b; border: 1px solid #2a2d35; border-radius: 8px; padding: 10px;
    }
    div[data-testid="stMetricValue"] { color: #f3f4f6; }
    div[data-testid="stMetricLabel"] { color: #9ca3af; }

    .streamlit-expanderHeader {
        background: #22252b !important; border: 1px solid #2a2d35 !important; border-radius: 8px !important;
    }
</style>
""", unsafe_allow_html=True)


# ══════════════════════════════════════════════════════════════════════════════
# DATA HELPERS
# ══════════════════════════════════════════════════════════════════════════════
def db_query(sql: str, params: tuple = ()) -> pd.DataFrame:
    conn = sqlite3.connect(DB_PATH)
    df   = pd.read_sql_query(sql, conn, params=params)
    conn.close()
    return df


@st.cache_data(ttl=30)
def get_live_price(address: str) -> float:
    try:
        r = requests.get(
            f"https://api.dexscreener.com/latest/dex/tokens/{address}", timeout=5
        )
        if r.status_code == 200:
            pairs = r.json().get("pairs", [])
            if pairs:
                return float(pairs[0].get("priceUsd") or 0)
    except Exception:
        pass
    return 0.0


@st.cache_data(ttl=30)
def get_token_full_info(address: str) -> dict:
    try:
        r = requests.get(
            f"https://api.dexscreener.com/latest/dex/tokens/{address}", timeout=5
        )
        if r.status_code == 200:
            pairs = r.json().get("pairs", [])
            if pairs:
                p = pairs[0]
                vol = p.get("volume", {})
                pc  = p.get("priceChange", {})
                txns = p.get("txns", {})
                return {
                    "symbol":     p.get("baseToken", {}).get("symbol", "?"),
                    "price":      float(p.get("priceUsd") or 0),
                    "change_5m":  float(pc.get("m5", 0) or 0),
                    "change_1h":  float(pc.get("h1", 0) or 0),
                    "change_6h":  float(pc.get("h6", 0) or 0),
                    "change_24h": float(pc.get("h24", 0) or 0),
                    "volume_1h":  float(vol.get("h1", 0) or 0),
                    "volume_24h": float(vol.get("h24", 0) or 0),
                    "liquidity":  float(p.get("liquidity", {}).get("usd", 0) or 0),
                    "market_cap": float(p.get("marketCap", 0) or 0),
                    "fdv":        float(p.get("fdv", 0) or 0),
                    "buys_1h":    int(txns.get("h1", {}).get("buys", 0) or 0),
                    "sells_1h":   int(txns.get("h1", {}).get("sells", 0) or 0),
                    "buys_24h":   int(txns.get("h24", {}).get("buys", 0) or 0),
                    "sells_24h":  int(txns.get("h24", {}).get("sells", 0) or 0),
                    "pair_created_at": p.get("pairCreatedAt", 0),
                    "dex_url":    p.get("url", ""),
                    "dex_id":     p.get("dexId", ""),
                }
    except Exception:
        pass
    return {}


@st.cache_data(ttl=60)
def get_sol_price() -> float:
    try:
        r = requests.get(
            "https://api.coingecko.com/api/v3/simple/price"
            "?ids=solana&vs_currencies=usd&include_24hr_change=true",
            timeout=5,
        )
        d = r.json()["solana"]
        return float(d["usd"])
    except Exception:
        return 0.0


@st.cache_data(ttl=60)
def get_sol_24h_change() -> float:
    try:
        r = requests.get(
            "https://api.coingecko.com/api/v3/simple/price"
            "?ids=solana&vs_currencies=usd&include_24hr_change=true",
            timeout=5,
        )
        return float(r.json()["solana"].get("usd_24h_change", 0))
    except Exception:
        return 0.0


@st.cache_data(ttl=60)
def get_btc_price_and_change() -> tuple:
    try:
        r = requests.get(
            "https://api.coingecko.com/api/v3/simple/price"
            "?ids=bitcoin&vs_currencies=usd&include_24hr_change=true",
            timeout=5,
        )
        d = r.json()["bitcoin"]
        return float(d["usd"]), float(d.get("usd_24h_change", 0))
    except Exception:
        return 0.0, 0.0


@st.cache_data(ttl=30)
def get_wallet_sol_balance(wallet: str) -> float:
    from src.utils.rpc import rpc_call
    try:
        result = rpc_call("getBalance", [wallet])
        if result is not None:
            return result["value"] / 1e9
    except Exception:
        pass
    return 0.0


@st.cache_data(ttl=60)
def get_wallet_tokens(wallet: str) -> list:
    from src.utils.rpc import rpc_call
    try:
        result = rpc_call("getTokenAccountsByOwner", [
            wallet,
            {"programId": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"},
            {"encoding": "jsonParsed"},
        ])
        if not result:
            return []
        tokens = []
        for acc in result.get("value", []):
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
    from src.utils.rpc import rpc_call
    try:
        result = rpc_call("getSignaturesForAddress", [wallet, {"limit": limit}])
        if not result:
            return []
        txs = []
        for sig in result:
            txs.append({
                "signature": sig.get("signature", ""),
                "time": (
                    datetime.fromtimestamp(sig["blockTime"]).strftime("%Y-%m-%d %H:%M")
                    if sig.get("blockTime") else "-"
                ),
                "status":  "OK" if not sig.get("err") else "Error",
                "slot":    sig.get("slot", ""),
            })
        return txs
    except Exception:
        return []


def load_positions() -> dict:
    if os.path.exists("positions.json"):
        try:
            with open("positions.json") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def fmt_usd(v: float, decimals: int = 2) -> str:
    if abs(v) < 0.01:
        return f"${v:.6f}"
    return f"${v:,.{decimals}f}"


def fmt_pct(v: float) -> str:
    return f"{v:+.2f}%"


def pl_color(v: float) -> str:
    if v > 0: return "profit"
    if v < 0: return "loss"
    return ""


def kpi_card(label: str, value: str, sub: str = "") -> str:
    return f"""
    <div class="kpi-card">
        <div class="label">{label}</div>
        <div class="value">{value}</div>
        <div class="sub">{sub}</div>
    </div>"""


def _generate_strategy_insight(dec, stage, n_passed, ai, rej, age_h):
    """Generate a strategy insight based on why the token was accepted/rejected."""
    hints = []
    md = ai.get("market_data", {}) if ai else {}
    cd = ai.get("chain_data", {}) if ai else {}
    flags = ai.get("risk_flags", []) if ai else []
    is_mig = ai.get("is_migration", False) if ai else False

    if "BUY" in dec:
        hints.append("Dieser Token hat alle 6 Gates bestanden und wurde gekauft.")
        if ai.get("hype_score", 0) >= 80:
            hints.append("Sehr hoher Hype-Score — starkes Momentum zum Kaufzeitpunkt.")
        if is_mig:
            hints.append("Migration-Token: Pump.fun Bonus +15 hat beim Scoring geholfen.")
        if md.get("liquidity_usd", 0) < 15000:
            hints.append("Achtung: Niedrige Liquiditaet erhoet das Slippage-Risiko.")

    elif stage == "DATA_CHECK":
        hints.append("Token hatte keine DexScreener-Daten. Entweder zu neu oder nicht gelistet.")
        if is_mig:
            hints.append("Migration-Tokens brauchen oft 1-3 Min bis DexScreener sie indexiert. Die Watchlist sollte das abfangen.")

    elif stage == "SAFETY_CHECK":
        hints.append("RugCheck hat diesen Token als unsicher eingestuft.")
        hints.append("Moegliche Gruende: Mint Authority nicht revoked, Freeze Authority aktiv, bekannter Scam.")
        hints.append("Strategie-Tipp: Safety ist ein Hard-Filter — daran sollte man nichts aendern.")

    elif stage == "PRE_FILTER":
        if "Liq zu niedrig" in rej or "Migration Liq" in rej:
            hints.append("Liquiditaet war unter dem Minimum. Niedrige Liq = hohes Rug-Pull Risiko.")
            hints.append("Wenn du mehr Risiko eingehen willst, koenntest du das Liq-Minimum senken — aber Vorsicht.")
        elif "fällt" in rej or "dumpt" in rej:
            hints.append("Token war zum Scan-Zeitpunkt im Abwaertstrend. Der Bot kauft nicht in fallende Messer.")
            hints.append("Strategie: Richtig so. Erst auf Trendumkehr warten.")
        elif "Spike zu niedrig" in rej:
            hints.append("Kein ausreichender Volume-Spike. Ohne Volumen-Explosion kein frühes Kaufsignal.")
        elif "Verkaufsdruck" in rej or "Kaufdruck" in rej:
            hints.append("Mehr Sells als Buys — der Markt verkauft diesen Token aktiv.")
            hints.append("Strategie: Buy-Ratio ist ein wichtiger Indikator fuer Momentum.")
        elif "zu neu" in rej.lower():
            hints.append("Token war < 1h alt. Sehr neue Tokens haben ein hohes Rug-Risiko.")
            hints.append("Migration-Tokens umgehen diesen Filter — sie sind neu by design.")
        elif "Critical Flag" in rej:
            flag_name = rej.split("Critical Flag: ")[-1].split("|")[0].strip() if "Critical Flag:" in rej else ""
            hints.append(f"Risk Flag '{flag_name}' hat den Token geblockt.")
            if "Heavy_Selling" in rej:
                hints.append("Mehr als doppelt so viele Sells wie Buys — klares Dump-Signal.")
            if "Wash_Trading" in rej:
                hints.append("Hohe Volume bei sehr wenigen Transaktionen = kuenstliches Volumen.")
            if "Liquidity_Drain" in rej:
                hints.append("Preis crasht und Volumen >> Liquiditaet — jemand zieht Liquiditaet ab.")
        else:
            hints.append(f"PreFilter hat geblockt: {rej[:80]}")

    elif stage == "SCORING":
        if "Override" in rej:
            override_part = rej.split("Override: ")[1].split("|")[0].strip() if "Override:" in rej else ""
            hints.append(f"Score war evtl. hoch genug, aber Override hat eingehriffen: {override_part}")
            hints.append("Overrides schuetzen vor Kaeufen trotz guten Scores bei kritischen Bedingungen.")
        else:
            hints.append(f"Fusion Score hat nicht gereicht (min 65 fuer BUY).")
            # Identify weakest signal
            if "Breakdown:" in rej:
                hints.append("Schau dir die Breakdown-Werte an — der niedrigste Wert ist dein Schwachpunkt.")
            if md.get("change_1h", 0) < 5:
                hints.append("Schwaches 1h-Momentum reduziert den Hype-Score stark.")
            if cd.get("top_10_pct", 0) > 50:
                hints.append("Hohe Wallet-Konzentration (Top 10) drueckt den Score runter.")

    elif "STOP_LOSS" in stage:
        hints.append("Position wurde per Stop-Loss geschlossen. Verlust begrenzt.")
        hints.append("Frage dich: War der Einstiegszeitpunkt richtig? Haetten die Daten beim Kauf schon Warnsignale gezeigt?")

    elif "TP" in stage:
        hints.append("Take-Profit erreicht — Gewinne realisiert!")
        if "TP3" in stage:
            hints.append("+200% ist ein exzellenter Trade. Die Strategie hat hier perfekt funktioniert.")
        elif "TP1" in stage:
            hints.append("50% der Position bei +50% verkauft. Restposition laeuft mit Trailing Stop weiter.")

    elif "TRAILING" in stage:
        hints.append("Trailing Stop hat ausgeloest. Der Token ist vom Hoechststand zurueckgefallen.")
        hints.append("Das ist gewollt: Gewinne sichern, bevor sie verschwinden.")

    elif "TIME_EXIT" in stage:
        hints.append("Position nach 24h ohne ausreichend Gewinn geschlossen.")
        hints.append("Stale Positionen binden Kapital. Time-Exit raeumt sie auf.")

    return " ".join(hints) if hints else ""


# ══════════════════════════════════════════════════════════════════════════════
# LOAD DATA
# ══════════════════════════════════════════════════════════════════════════════
positions  = load_positions()
sol_bal    = get_wallet_sol_balance(WALLET_ADDRESS)
sol_price  = get_sol_price()
sol_change = get_sol_24h_change()
btc_price, btc_change = get_btc_price_and_change()
sol_usd    = sol_bal * sol_price


# ══════════════════════════════════════════════════════════════════════════════
# SIDEBAR
# ══════════════════════════════════════════════════════════════════════════════
with st.sidebar:
    st.markdown("### Memecoin Terminal")
    st.caption(datetime.now().strftime("%Y-%m-%d  %H:%M:%S"))

    # Bot status
    bot_stopped = os.path.exists("STOP_BOT")
    if bot_stopped:
        st.markdown(
            '<span class="status-dot status-stopped"></span> <b>Bot Offline</b>',
            unsafe_allow_html=True,
        )
        if st.button("Start Bot", use_container_width=True, type="primary"):
            os.remove("STOP_BOT")
            st.rerun()
    else:
        st.markdown(
            '<span class="status-dot status-running"></span> <b>Bot Running</b>',
            unsafe_allow_html=True,
        )
        if st.button("Stop Bot", use_container_width=True):
            open("STOP_BOT", "w").write("STOP")
            st.rerun()

    st.divider()

    # Wallet summary
    st.markdown("**Wallet**")
    st.metric("SOL Balance", f"{sol_bal:.4f} SOL", f"{fmt_usd(sol_usd)}")
    st.metric("SOL Price", fmt_usd(sol_price), fmt_pct(sol_change))
    st.metric("Positions", f"{len(positions)} / 20")

    st.divider()

    # Market
    st.markdown("**Market**")
    st.metric("BTC", fmt_usd(btc_price, 0), fmt_pct(btc_change))

    st.divider()

    # Strategy params
    with st.expander("Strategy Config"):
        st.caption(f"Stop-Loss: -{int(STOP_LOSS_PCT*100)}%")
        st.caption(f"Trailing: -{int(TRAILING_STOP_PCT*100)}% (activates at +{int(TRAILING_ACTIVATE*100)}%)")
        st.caption(f"TP1: +{int(TP1_PCT*100)}% | TP2: +{int(TP2_PCT*100)}% | TP3: +{int(TP3_PCT*100)}%")
        st.caption(f"Max Hold: {MAX_HOLD_HOURS}h")
        st.caption(f"Position Size: {fmt_usd(POSITION_SIZE_USD)}")

    if st.button("Refresh All", use_container_width=True):
        st.cache_data.clear()
        st.rerun()

    st.markdown(
        f"[Solscan Wallet](https://solscan.io/account/{WALLET_ADDRESS})"
    )


# ══════════════════════════════════════════════════════════════════════════════
# TABS
# ══════════════════════════════════════════════════════════════════════════════
tab_overview, tab_positions, tab_trade, tab_wallet, tab_history, tab_analytics, tab_logs = st.tabs([
    "Overview",
    "Positions",
    "Trade",
    "Wallet",
    "History",
    "Analytics",
    "Logs",
])


# ══════════════════════════════════════════════════════════════════════════════
# TAB 1 — OVERVIEW / COMMAND CENTER
# ══════════════════════════════════════════════════════════════════════════════
with tab_overview:
    # Compute P/L
    total_pl_usd      = 0.0
    total_invested     = 0.0
    total_current_val  = 0.0
    winning_positions  = 0
    losing_positions   = 0

    for addr, pos in positions.items():
        ep = float(pos.get("entry_price", 0))
        cp = get_live_price(addr)
        remaining = float(pos.get("remaining_pct", 1.0))
        ps = POSITION_SIZE_USD * remaining
        if ep > 0 and cp > 0:
            cur_val = (ps / ep) * cp
            pl = cur_val - ps
            total_pl_usd += pl
            total_invested += ps
            total_current_val += cur_val
            if pl > 0:
                winning_positions += 1
            elif pl < 0:
                losing_positions += 1

    total_pl_pct = (total_pl_usd / total_invested * 100) if total_invested > 0 else 0

    # DB stats
    df_stats = db_query("SELECT decision, funnel_stage FROM trades")
    total_buys  = df_stats["decision"].str.contains("BUY", na=False).sum()
    total_sells = df_stats["decision"].str.contains("SELL", na=False).sum()
    total_scanned = len(df_stats)

    # KPI Row
    c1, c2, c3, c4, c5, c6 = st.columns(6)
    with c1:
        color = "profit" if sol_usd > 0 else ""
        st.markdown(kpi_card("Wallet Balance", f"{sol_bal:.4f} SOL", fmt_usd(sol_usd)), unsafe_allow_html=True)
    with c2:
        st.markdown(kpi_card(
            "Portfolio P/L",
            f'<span class="{pl_color(total_pl_usd)}">{fmt_usd(total_pl_usd)}</span>',
            f'{fmt_pct(total_pl_pct)}',
        ), unsafe_allow_html=True)
    with c3:
        st.markdown(kpi_card("Open Positions", f"{len(positions)}", f"{winning_positions} winning / {losing_positions} losing"), unsafe_allow_html=True)
    with c4:
        st.markdown(kpi_card("Invested", fmt_usd(total_invested), f"Value: {fmt_usd(total_current_val)}"), unsafe_allow_html=True)
    with c5:
        st.markdown(kpi_card("Total Buys", str(total_buys), f"of {total_scanned} scanned"), unsafe_allow_html=True)
    with c6:
        st.markdown(kpi_card("Total Sells", str(total_sells), ""), unsafe_allow_html=True)

    st.markdown("")

    # Open Positions Quick View
    st.markdown('<div class="section-header">Open Positions</div>', unsafe_allow_html=True)

    if positions:
        for addr, pos in positions.items():
            ep    = float(pos.get("entry_price", 0))
            sym   = pos.get("symbol", addr[:8])
            rem   = float(pos.get("remaining_pct", 1.0))
            cp    = get_live_price(addr)
            ath   = float(pos.get("highest_price", ep))
            trail = pos.get("trailing_active", False)

            pl_pct = ((cp - ep) / ep * 100) if ep > 0 and cp > 0 else 0
            pl_usd = (POSITION_SIZE_USD * rem / ep * cp - POSITION_SIZE_USD * rem) if ep > 0 and cp > 0 else 0
            created = pos.get("created_at", 0)
            age_h   = (time.time() - created) / 3600 if created else 0

            badge_class = "badge-profit" if pl_pct > 0 else ("badge-loss" if pl_pct < 0 else "badge-neutral")
            badge_text  = f"{pl_pct:+.1f}%" if cp > 0 else "N/A"

            tp1 = "hit" if pos.get("tp1_hit") else "pending"
            tp2 = "hit" if pos.get("tp2_hit") else "pending"

            st.markdown(f"""
            <div class="pos-card">
                <div class="pos-header">
                    <span class="pos-symbol">{sym}</span>
                    <span class="pos-badge {badge_class}">{badge_text}</span>
                </div>
                <div class="pos-grid">
                    <div class="pos-item">
                        <div class="lbl">Entry</div>
                        <div class="val">${ep:.8f}</div>
                    </div>
                    <div class="pos-item">
                        <div class="lbl">Current</div>
                        <div class="val {'profit' if pl_pct > 0 else 'loss' if pl_pct < 0 else ''}">${cp:.8f}</div>
                    </div>
                    <div class="pos-item">
                        <div class="lbl">P/L USD</div>
                        <div class="val {'profit' if pl_usd > 0 else 'loss' if pl_usd < 0 else ''}">{fmt_usd(pl_usd)}</div>
                    </div>
                    <div class="pos-item">
                        <div class="lbl">ATH</div>
                        <div class="val">${ath:.8f}</div>
                    </div>
                    <div class="pos-item">
                        <div class="lbl">Remaining</div>
                        <div class="val">{int(rem*100)}%</div>
                    </div>
                    <div class="pos-item">
                        <div class="lbl">Age</div>
                        <div class="val">{age_h:.1f}h</div>
                    </div>
                    <div class="pos-item">
                        <div class="lbl">TP1 / TP2</div>
                        <div class="val">{'OK' if tp1 == 'hit' else '-'} / {'OK' if tp2 == 'hit' else '-'}</div>
                    </div>
                    <div class="pos-item">
                        <div class="lbl">Trailing</div>
                        <div class="val {'profit' if trail else ''}">{'Active' if trail else 'Inactive'}</div>
                    </div>
                </div>
            </div>
            """, unsafe_allow_html=True)
    else:
        st.info("No open positions.")

    # Recent Activity
    st.markdown("")
    st.markdown('<div class="section-header">Recent Bot Activity</div>', unsafe_allow_html=True)
    df_recent = db_query("""
        SELECT level, message, timestamp FROM bot_logs
        ORDER BY timestamp DESC LIMIT 15
    """)
    if not df_recent.empty:
        for _, row in df_recent.iterrows():
            lvl = row["level"]
            css = {"ERROR": "log-error", "WARNING": "log-warning", "SUCCESS": "log-success"}.get(lvl, "log-info")
            ts  = str(row["timestamp"])[-8:]
            st.markdown(
                f'<span class="{css}" style="font-family: monospace; font-size: 0.8rem;">'
                f'[{ts}] [{lvl}] {row["message"]}</span>',
                unsafe_allow_html=True,
            )


# ══════════════════════════════════════════════════════════════════════════════
# TAB 2 — POSITIONS (detailed + interactive)
# ══════════════════════════════════════════════════════════════════════════════
with tab_positions:
    st.markdown('<div class="section-header">Active Positions - Details</div>', unsafe_allow_html=True)

    if not positions:
        st.info("No open positions.")
    else:
        for addr, pos in positions.items():
            ep    = float(pos.get("entry_price", 0))
            sym   = pos.get("symbol", addr[:8])
            rem   = float(pos.get("remaining_pct", 1.0))
            cp    = get_live_price(addr)
            ath   = float(pos.get("highest_price", ep))
            trail = pos.get("trailing_active", False)
            created = pos.get("created_at", 0)
            age_h   = (time.time() - created) / 3600 if created else 0

            pl_pct = ((cp - ep) / ep * 100) if ep > 0 and cp > 0 else 0
            pl_usd = (POSITION_SIZE_USD * rem / ep * cp - POSITION_SIZE_USD * rem) if ep > 0 and cp > 0 else 0
            invested = POSITION_SIZE_USD * rem
            cur_val  = (invested / ep * cp) if ep > 0 and cp > 0 else 0

            pl_emoji = "+" if pl_pct > 0 else ""
            with st.expander(f"**{sym}**  |  {pl_emoji}{pl_pct:.1f}%  |  {fmt_usd(pl_usd)}  |  Age: {age_h:.1f}h", expanded=len(positions) <= 5):
                # Token live data
                info = get_token_full_info(addr)

                col_l, col_r = st.columns([3, 2])

                with col_l:
                    # Price info
                    m1, m2, m3, m4 = st.columns(4)
                    m1.metric("Entry Price", f"${ep:.8f}")
                    m2.metric("Current Price", f"${cp:.8f}", fmt_pct(pl_pct))
                    m3.metric("ATH", f"${ath:.8f}")
                    m4.metric("P/L", fmt_usd(pl_usd), fmt_pct(pl_pct))

                    # Position details
                    m5, m6, m7, m8 = st.columns(4)
                    m5.metric("Invested", fmt_usd(invested))
                    m6.metric("Current Value", fmt_usd(cur_val))
                    m7.metric("Remaining", f"{int(rem*100)}%")
                    m8.metric("Age", f"{age_h:.1f}h")

                with col_r:
                    if info:
                        st.markdown("**Market Data**")
                        st.caption(f"Liquidity: {fmt_usd(info.get('liquidity', 0))}")
                        st.caption(f"Market Cap: {fmt_usd(info.get('market_cap', 0))}")
                        st.caption(f"Volume 24h: {fmt_usd(info.get('volume_24h', 0))}")
                        st.caption(f"5m: {fmt_pct(info.get('change_5m', 0))} | 1h: {fmt_pct(info.get('change_1h', 0))} | 24h: {fmt_pct(info.get('change_24h', 0))}")
                        buys = info.get("buys_1h", 0)
                        sells = info.get("sells_1h", 0)
                        total = buys + sells
                        ratio = (buys / total * 100) if total > 0 else 50
                        st.caption(f"Buy/Sell 1h: {buys}/{sells} ({ratio:.0f}% buys)")
                        st.caption(f"DEX: {info.get('dex_id', '?')}")

                    # TP/SL visual
                    st.markdown("**Exit Levels**")
                    sl_price  = ep * (1 - STOP_LOSS_PCT)
                    tp1_price = ep * (1 + TP1_PCT)
                    tp2_price = ep * (1 + TP2_PCT)
                    tp3_price = ep * (1 + TP3_PCT)
                    tp1_hit = "HIT" if pos.get("tp1_hit") else f"${tp1_price:.8f}"
                    tp2_hit = "HIT" if pos.get("tp2_hit") else f"${tp2_price:.8f}"

                    st.caption(f"Stop-Loss: ${sl_price:.8f} (-{int(STOP_LOSS_PCT*100)}%)")
                    st.caption(f"TP1: {tp1_hit} (+{int(TP1_PCT*100)}%)")
                    st.caption(f"TP2: {tp2_hit} (+{int(TP2_PCT*100)}%)")
                    st.caption(f"TP3: ${tp3_price:.8f} (+{int(TP3_PCT*100)}%)")
                    st.caption(f"Trailing: {'ACTIVE' if trail else 'Inactive (activates at +' + str(int(TRAILING_ACTIVATE*100)) + '%)'}")
                    st.caption(f"Time Exit: {MAX_HOLD_HOURS}h ({MAX_HOLD_HOURS - age_h:.1f}h remaining)")

                # Links + Manual Sell
                link_col, sell_col = st.columns([3, 2])
                with link_col:
                    dex_url = info.get("dex_url", "") if info else ""
                    st.markdown(
                        f"[DexScreener]({dex_url}) | "
                        f"[Solscan](https://solscan.io/token/{addr}) | "
                        f"`{addr[:20]}...`"
                    )
                with sell_col:
                    sell_pct = st.selectbox(
                        "Sell %", [25, 50, 75, 100],
                        index=3, key=f"sell_{addr}",
                    )
                    if st.button(f"SELL {sell_pct}%", key=f"sellbtn_{addr}", type="primary"):
                        trigger = {"action": "SELL", "address": addr, "sell_pct": sell_pct / 100}
                        with open("MANUAL_TRADE", "w") as f:
                            json.dump(trigger, f)
                        st.success(f"Manual SELL {sell_pct}% queued for {sym}")


# ══════════════════════════════════════════════════════════════════════════════
# TAB 3 — MANUAL TRADE
# ══════════════════════════════════════════════════════════════════════════════
with tab_trade:
    st.markdown('<div class="section-header">Manual Trade</div>', unsafe_allow_html=True)

    trade_col1, trade_col2 = st.columns([1, 1])

    with trade_col1:
        st.markdown("**Buy Token**")
        buy_address = st.text_input("Token Address (Solana)", placeholder="Enter mint address...", key="buy_addr")
        buy_amount  = st.number_input("Amount (USD)", min_value=0.01, max_value=100.0, value=POSITION_SIZE_USD, step=0.1, key="buy_amt")

        if buy_address:
            with st.spinner("Fetching token data..."):
                token_info = get_token_full_info(buy_address)

            if token_info and token_info.get("price", 0) > 0:
                st.markdown(f"**{token_info['symbol']}** - ${token_info['price']:.8f}")

                ti1, ti2, ti3, ti4 = st.columns(4)
                ti1.metric("Liquidity", fmt_usd(token_info.get("liquidity", 0)))
                ti2.metric("Market Cap", fmt_usd(token_info.get("market_cap", 0)))
                ti3.metric("1h Change", fmt_pct(token_info.get("change_1h", 0)))
                ti4.metric("24h Volume", fmt_usd(token_info.get("volume_24h", 0)))

                buys = token_info.get("buys_1h", 0)
                sells = token_info.get("sells_1h", 0)
                total = buys + sells
                if total > 0:
                    buy_ratio = buys / total * 100
                    st.progress(buy_ratio / 100, text=f"Buy Pressure: {buy_ratio:.0f}% ({buys} buys / {sells} sells)")

                # Warnings
                liq = token_info.get("liquidity", 0)
                if liq < 5000:
                    st.warning(f"Low liquidity: {fmt_usd(liq)}")
                if token_info.get("change_1h", 0) < -10:
                    st.warning(f"Token falling: 1h {fmt_pct(token_info['change_1h'])}")
                if token_info.get("change_24h", 0) > 500:
                    st.warning(f"Already mooned: 24h {fmt_pct(token_info['change_24h'])}")

                st.markdown(f"[DexScreener]({token_info.get('dex_url', '')}) | [Solscan](https://solscan.io/token/{buy_address})")

                if st.button("BUY", type="primary", key="exec_buy", use_container_width=True):
                    trigger = {"action": "BUY", "address": buy_address, "amount": buy_amount}
                    with open("MANUAL_TRADE", "w") as f:
                        json.dump(trigger, f)
                    st.success(f"Manual BUY queued: {token_info['symbol']} for {fmt_usd(buy_amount)}")
            elif buy_address:
                st.error("Token not found or no trading pairs available.")

    with trade_col2:
        st.markdown("**Quick Sell Position**")
        if positions:
            pos_options = {
                f"{pos.get('symbol', addr[:8])} ({addr[:12]}...)": addr
                for addr, pos in positions.items()
            }
            selected = st.selectbox("Select Position", list(pos_options.keys()), key="quick_sell_select")
            if selected:
                sell_addr = pos_options[selected]
                sell_pos  = positions[sell_addr]
                ep = float(sell_pos.get("entry_price", 0))
                cp = get_live_price(sell_addr)
                pl = ((cp - ep) / ep * 100) if ep > 0 and cp > 0 else 0

                st.metric("Current P/L", fmt_pct(pl), fmt_usd((POSITION_SIZE_USD / ep * cp - POSITION_SIZE_USD) if ep > 0 and cp > 0 else 0))

                sell_amount = st.slider("Sell Percentage", 10, 100, 100, 5, key="quick_sell_pct")
                if st.button(f"SELL {sell_amount}%", type="primary", key="exec_quick_sell", use_container_width=True):
                    trigger = {"action": "SELL", "address": sell_addr, "sell_pct": sell_amount / 100}
                    with open("MANUAL_TRADE", "w") as f:
                        json.dump(trigger, f)
                    st.success(f"Manual SELL {sell_amount}% queued for {sell_pos.get('symbol', '?')}")
        else:
            st.info("No open positions to sell.")


# ══════════════════════════════════════════════════════════════════════════════
# TAB 4 — WALLET
# ══════════════════════════════════════════════════════════════════════════════
with tab_wallet:
    st.markdown('<div class="section-header">Wallet Overview</div>', unsafe_allow_html=True)
    st.caption(f"`{WALLET_ADDRESS}`")
    st.markdown(f"[View on Solscan](https://solscan.io/account/{WALLET_ADDRESS})")

    w1, w2, w3, w4 = st.columns(4)
    w1.metric("SOL Balance", f"{sol_bal:.6f} SOL")
    w2.metric("USD Value", fmt_usd(sol_usd))
    w3.metric("SOL Price", fmt_usd(sol_price), fmt_pct(sol_change))
    w4.metric("BTC Price", fmt_usd(btc_price, 0), fmt_pct(btc_change))

    st.divider()

    # Token Holdings
    st.markdown('<div class="section-header">Token Holdings</div>', unsafe_allow_html=True)
    with st.spinner("Loading tokens..."):
        tokens = get_wallet_tokens(WALLET_ADDRESS)

    if tokens:
        token_rows = []
        total_token_value = 0
        for t in tokens:
            mint   = t["mint"]
            amount = t["amount"]
            info   = get_token_full_info(mint)
            price  = info.get("price", 0)
            sym    = info.get("symbol", mint[:8] + "...")
            value  = amount * price
            total_token_value += value
            token_rows.append({
                "Symbol":     sym,
                "Amount":     f"{amount:,.2f}",
                "Price":      f"${price:.10f}" if price > 0 else "-",
                "Value USD":  round(value, 4),
                "1h %":       round(info.get("change_1h", 0), 2),
                "24h %":      round(info.get("change_24h", 0), 2),
                "Liquidity":  fmt_usd(info.get("liquidity", 0)),
                "Address":    mint,
            })

        st.metric("Total Token Value", fmt_usd(total_token_value))

        # Make it clickable with expanders
        for row in token_rows:
            with st.expander(f"**{row['Symbol']}** | {row['Amount']} | Value: {fmt_usd(row['Value USD'])} | 1h: {fmt_pct(row['1h %'])}"):
                full_info = get_token_full_info(row["Address"])
                if full_info:
                    d1, d2, d3, d4 = st.columns(4)
                    d1.metric("Price", full_info.get("price", 0))
                    d2.metric("Liquidity", fmt_usd(full_info.get("liquidity", 0)))
                    d3.metric("Market Cap", fmt_usd(full_info.get("market_cap", 0)))
                    d4.metric("Volume 24h", fmt_usd(full_info.get("volume_24h", 0)))

                    d5, d6, d7, d8 = st.columns(4)
                    d5.metric("5m", fmt_pct(full_info.get("change_5m", 0)))
                    d6.metric("1h", fmt_pct(full_info.get("change_1h", 0)))
                    d7.metric("6h", fmt_pct(full_info.get("change_6h", 0)))
                    d8.metric("24h", fmt_pct(full_info.get("change_24h", 0)))

                st.markdown(f"[DexScreener]({full_info.get('dex_url', '') if full_info else ''}) | [Solscan](https://solscan.io/token/{row['Address']})")
                st.code(row["Address"], language=None)
    else:
        st.info("No tokens found in wallet.")

    st.divider()

    # Transaction History
    st.markdown('<div class="section-header">Transaction History</div>', unsafe_allow_html=True)
    n_tx = st.selectbox("Number of transactions", [10, 20, 50], index=1, key="tx_count")
    with st.spinner("Loading transactions from Solana..."):
        txs = get_wallet_transactions(WALLET_ADDRESS, limit=n_tx)

    if txs:
        df_tx = pd.DataFrame(txs)
        df_tx["Link"] = df_tx["signature"].apply(lambda s: f"https://solscan.io/tx/{s}")
        df_tx["TX"]   = df_tx["signature"].str[:24] + "..."

        st.dataframe(
            df_tx[["time", "TX", "status", "slot"]].rename(columns={
                "time": "Time", "TX": "Transaction", "status": "Status", "slot": "Block",
            }),
            use_container_width=True,
            height=min(80 + len(txs) * 38, 500),
        )
    else:
        st.info("No transactions found.")


# ══════════════════════════════════════════════════════════════════════════════
# TAB 5 — TRADING HISTORY (Professional)
# ══════════════════════════════════════════════════════════════════════════════
with tab_history:

    # ── Auto-refresh toggle ──────────────────────────────────────────────────
    hist_top_l, hist_top_r = st.columns([4, 1])
    with hist_top_l:
        st.markdown('<div class="section-header">Trading History</div>', unsafe_allow_html=True)
    with hist_top_r:
        auto_refresh = st.toggle("Auto-Refresh (30s)", value=False, key="hist_auto")

    if auto_refresh:
        import streamlit.components.v1 as components
        components.html(
            '<script>setTimeout(function(){window.parent.location.reload()},30000)</script>',
            height=0,
        )

    # ── Quick Stats Row ──────────────────────────────────────────────────────
    df_hist_stats = db_query("""
        SELECT
            COUNT(*) as total,
            SUM(CASE WHEN decision LIKE '%BUY%' THEN 1 ELSE 0 END) as buys,
            SUM(CASE WHEN decision LIKE '%SELL%' THEN 1 ELSE 0 END) as sells,
            SUM(CASE WHEN decision = 'REJECT' THEN 1 ELSE 0 END) as rejects,
            SUM(CASE WHEN funnel_stage = 'SAFETY_CHECK' THEN 1 ELSE 0 END) as g2_fail,
            SUM(CASE WHEN funnel_stage = 'PRE_FILTER' THEN 1 ELSE 0 END) as g4_fail,
            SUM(CASE WHEN funnel_stage = 'SCORING' THEN 1 ELSE 0 END) as g5_fail,
            SUM(CASE WHEN funnel_stage = 'BUY_EXEC' THEN 1 ELSE 0 END) as bought
        FROM trades
    """)
    if not df_hist_stats.empty:
        s = df_hist_stats.iloc[0]
        hs1, hs2, hs3, hs4, hs5, hs6 = st.columns(6)
        hs1.markdown(kpi_card("Scanned", str(int(s.get("total", 0))), ""), unsafe_allow_html=True)
        hs2.markdown(kpi_card("Bought", f'<span class="profit">{int(s.get("bought", 0))}</span>', "Passed all gates"), unsafe_allow_html=True)
        hs3.markdown(kpi_card("Sold", str(int(s.get("sells", 0))), ""), unsafe_allow_html=True)
        hs4.markdown(kpi_card("Safety Fail", str(int(s.get("g2_fail", 0))), "Gate 2"), unsafe_allow_html=True)
        hs5.markdown(kpi_card("PreFilter Fail", str(int(s.get("g4_fail", 0))), "Gate 4"), unsafe_allow_html=True)
        hs6.markdown(kpi_card("Score Fail", str(int(s.get("g5_fail", 0))), "Gate 5"), unsafe_allow_html=True)

    st.markdown("")

    # ── Filters ──────────────────────────────────────────────────────────────
    with st.container():
        fc1, fc2, fc3, fc4, fc5 = st.columns([2, 1.2, 1.2, 1.2, 0.8])
        with fc1:
            search = st.text_input(
                "Search", placeholder="Symbol or address...",
                key="hist_search", label_visibility="collapsed",
            )
        with fc2:
            decision_filter = st.selectbox(
                "Decision", ["All", "BUY", "SELL", "REJECT", "SKIP"],
                key="hist_dec",
            )
        with fc3:
            stage_filter = st.selectbox(
                "Failed at Gate",
                ["All", "DATA_CHECK", "SAFETY_CHECK", "PRE_FILTER", "SCORING",
                 "BUY_EXEC", "EXEC_LIMIT",
                 "STOP_LOSS", "TP1", "TP2", "TP3", "TRAILING_STOP", "TIME_EXIT"],
                key="hist_stage",
            )
        with fc4:
            gate_filter = st.selectbox(
                "Min Gates Passed",
                ["Any", "1+ (Data)", "2+ (Safety)", "3+ (Risk)", "4+ (PreFilter)", "5+ (Scoring)", "6 (Bought)"],
                key="hist_gate",
            )
        with fc5:
            limit = st.selectbox("Rows", [25, 50, 100, 250, 500], index=1, key="hist_limit")

    # Build query
    where_clauses = []
    params = []
    if search:
        where_clauses.append("(symbol LIKE ? OR token_address LIKE ?)")
        params.extend([f"%{search}%", f"%{search}%"])
    if decision_filter != "All":
        where_clauses.append("decision LIKE ?")
        params.append(f"%{decision_filter}%")
    if stage_filter != "All":
        where_clauses.append("funnel_stage = ?")
        params.append(stage_filter)

    # Gate filter — count commas in gates_passed
    gate_min_map = {
        "1+ (Data)": 1, "2+ (Safety)": 2, "3+ (Risk)": 3,
        "4+ (PreFilter)": 4, "5+ (Scoring)": 5, "6 (Bought)": 6,
    }
    if gate_filter != "Any" and gate_filter in gate_min_map:
        min_gates = gate_min_map[gate_filter]
        # gates_passed has format "G1:Data,G2:Safety,..." — count by commas
        if min_gates == 1:
            where_clauses.append("gates_passed IS NOT NULL AND gates_passed != ''")
        else:
            # length(gates_passed) - length(replace(gates_passed,',','')) counts commas
            # commas = gates - 1, so gates >= min_gates means commas >= min_gates - 1
            where_clauses.append(
                f"(LENGTH(gates_passed) - LENGTH(REPLACE(gates_passed, ',', ''))) >= {min_gates - 1}"
            )

    where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""

    df_hist = db_query(
        f"""
        SELECT id, symbol, token_address, entry_price, position_size,
               score, decision, rejection_reason, ai_reasoning,
               funnel_stage, gates_passed, timestamp
        FROM trades
        {where_sql}
        ORDER BY timestamp DESC
        LIMIT ?
        """,
        tuple(params + [limit]),
    )

    # ── Render History Cards ─────────────────────────────────────────────────
    if not df_hist.empty:
        st.caption(f"Showing {len(df_hist)} trades")

        for idx, row in df_hist.iterrows():
            sym    = str(row["symbol"] or "UNKNOWN")
            addr   = str(row["token_address"] or "")
            ep     = float(row["entry_price"] or 0)
            score  = float(row["score"] or 0)
            dec    = str(row["decision"] or "")
            stage  = str(row["funnel_stage"] or "")
            ts     = str(row["timestamp"] or "")[:19]
            rej    = str(row["rejection_reason"] or "")
            gates  = str(row["gates_passed"] or "")
            tid    = int(row["id"] or 0)
            ai_raw = str(row["ai_reasoning"] or "")

            # Parse AI data once for card + detail
            ai = {}
            if ai_raw:
                try:
                    ai = json.loads(ai_raw)
                except Exception:
                    pass

            # Token timing
            pair_created_ms  = ai.get("pair_created_at", 0)
            token_age_h      = ai.get("token_age_hours", -1)
            scanned_at       = ai.get("scanned_at", 0)
            source_tag       = ai.get("source", "")
            is_mig           = ai.get("is_migration", False)
            pumpfun_detected = ai.get("pumpfun_detected_at", 0)

            created_str = ""
            if pair_created_ms and pair_created_ms > 0:
                try:
                    created_dt = datetime.fromtimestamp(pair_created_ms / 1000)
                    created_str = created_dt.strftime("%Y-%m-%d %H:%M")
                except Exception:
                    pass

            age_str = ""
            if token_age_h >= 0:
                if token_age_h < 1:
                    age_str = f"{token_age_h * 60:.0f}min"
                elif token_age_h < 24:
                    age_str = f"{token_age_h:.1f}h"
                else:
                    age_str = f"{token_age_h / 24:.1f}d"

            # Gates
            all_gates = ["G1:Data", "G2:Safety", "G3:Risk", "G4:PreFilter", "G5:Scoring", "G6:Exec"]
            passed_list = [g.strip() for g in gates.split(",") if g.strip()] if gates else []
            n_passed = len(passed_list)

            gate_html = ""
            for g in all_gates:
                g_short = g.split(":")[0]
                cls = "gate-pass" if g in passed_list else "gate-fail"
                gate_html += '<span class="' + cls + '">' + g_short + '</span>'

            # Decision badge
            if "BUY" in dec:
                dec_cls = "badge-profit"
            elif "SELL" in dec:
                dec_cls = "badge-neutral"
            else:
                dec_cls = "badge-loss"

            # Source label
            src_html = ""
            if source_tag:
                src_label = "MIGRATION" if is_mig else source_tag
                src_cls = "gate-pass" if is_mig else "info-badge"
                src_html = '<span class="' + src_cls + '">' + src_label + '</span>'

            # Score
            if score >= 65:     s_cls = "profit"
            elif score > 0:     s_cls = "loss"
            else:               s_cls = ""

            addr_short = (addr[:8] + "..." + addr[-6:]) if len(addr) > 14 else addr
            reason_safe = (rej if rej else "-").replace("<", "&lt;").replace(">", "&gt;")

            # Discovery delay
            delay_html = ""
            if scanned_at and pair_created_ms and pair_created_ms > 0:
                delay_sec = scanned_at - (pair_created_ms / 1000)
                if delay_sec > 0:
                    if delay_sec < 60:      d_str = str(int(delay_sec)) + "s"
                    elif delay_sec < 3600:  d_str = str(round(delay_sec / 60, 1)) + "min"
                    else:                   d_str = str(round(delay_sec / 3600, 1)) + "h"
                    delay_html = '<span class="info-badge">Delay: ' + d_str + '</span>'

            # ── Card: 2 rows ─────────────────────────────────────────────
            card = []
            card.append('<div class="hist-card">')

            # Row 1: Symbol | Decision | Source | Score | Gates | ID
            card.append('<div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-bottom:6px">')
            card.append('<b style="color:#f3f4f6;font-size:1.05rem">' + sym + '</b>')
            card.append('<span class="pos-badge ' + dec_cls + '">' + dec + '</span>')
            card.append(src_html)
            if score > 0:
                card.append('<span class="' + s_cls + '" style="font-size:0.85rem;font-weight:700">' + str(round(score, 1)) + '</span>')
            card.append('<span style="flex:1"></span>')  # spacer
            card.append(gate_html)
            card.append('<span style="color:#6b7280;font-size:0.7rem">#' + str(tid) + '</span>')
            card.append('</div>')

            # Row 2: Address | Stage | Timestamps | Age | Delay
            card.append('<div style="display:flex;align-items:center;gap:6px;flex-wrap:wrap;margin-bottom:6px">')
            card.append('<span class="info-badge">' + addr_short + '</span>')
            card.append('<span class="info-badge">' + stage + '</span>')
            card.append('<span class="info-badge">' + ts + '</span>')
            if created_str:
                card.append('<span class="info-badge">Created: ' + created_str + '</span>')
            if age_str:
                card.append('<span class="info-badge">Age: ' + age_str + '</span>')
            if delay_html:
                card.append(delay_html)
            card.append('</div>')

            # Row 3: Reason (truncated)
            card.append('<div style="color:#9ca3af;font-size:0.78rem;line-height:1.4;word-break:break-word">')
            card.append(reason_safe[:250])
            card.append('</div>')

            card.append('</div>')
            st.markdown("".join(card), unsafe_allow_html=True)

            # ── Expandable Detail ────────────────────────────────────────
            with st.expander("Details  |  " + sym + "  #" + str(tid), expanded=False):
                det_l, det_m, det_r = st.columns([2.5, 2, 1.5])

                with det_l:
                    st.markdown("**Decision Reason**")
                    if rej:
                        st.code(rej, language=None)
                    else:
                        st.caption("No reason logged")

                    if ai:
                        signals = ai.get("key_signals", [])
                        if signals:
                            st.markdown("**Signals at Scan**")
                            for sig in signals:
                                st.markdown("- `" + str(sig) + "`")

                        flags = ai.get("risk_flags", [])
                        if flags:
                            is_clean = flags == ["No_Risk_Flags"]
                            color = "#4ade80" if is_clean else "#f87171"
                            st.markdown(
                                '<span style="color:' + color + '">' + ", ".join(flags) + '</span>',
                                unsafe_allow_html=True,
                            )
                        st.caption("Hype: " + str(ai.get("hype_score", "?")) + "/100  |  " + str(ai.get("sentiment", "?")))

                with det_m:
                    st.markdown("**Token Timeline**")

                    tl_rows = []

                    # 1. Pair creation (DexScreener)
                    if created_str:
                        tl_rows.append("DexScreener pair created: **" + created_str + "**")
                    else:
                        tl_rows.append("DexScreener pair created: _not available_")

                    # 2. PumpPortal detection
                    if pumpfun_detected and pumpfun_detected > 0:
                        pf_dt = datetime.fromtimestamp(pumpfun_detected).strftime("%Y-%m-%d %H:%M:%S")
                        tl_rows.append("PumpPortal detected: **" + pf_dt + "**")
                        # Delay between PumpPortal and DexScreener
                        if pair_created_ms and pair_created_ms > 0:
                            pf_to_dex = (pair_created_ms / 1000) - pumpfun_detected
                            if abs(pf_to_dex) < 86400:  # only if within 24h
                                if pf_to_dex > 0:
                                    tl_rows.append("PumpPortal -> DexScreener: **" + str(round(pf_to_dex / 60, 1)) + " min**")
                                else:
                                    tl_rows.append("DexScreener had data **before** PumpPortal event")

                    # 3. Token age
                    if age_str:
                        tl_rows.append("Token age at scan: **" + age_str + "**")

                    # 4. Bot scan time
                    tl_rows.append("Bot scanned at: **" + ts + "**")

                    # 5. Discovery delay (bot scan vs creation)
                    if scanned_at and pair_created_ms and pair_created_ms > 0:
                        delay_sec = scanned_at - (pair_created_ms / 1000)
                        if delay_sec > 0:
                            if delay_sec < 60:      d_str = str(int(delay_sec)) + "s"
                            elif delay_sec < 3600:  d_str = str(round(delay_sec / 60, 1)) + " min"
                            else:                   d_str = str(round(delay_sec / 3600, 1)) + "h"
                            tl_rows.append("Discovery delay: **" + d_str + "** (creation to scan)")

                    # 6. Source
                    if is_mig:
                        tl_rows.append("Source: **PumpPortal WebSocket** (Migration)")
                    elif source_tag:
                        tl_rows.append("Source: **DexScreener** (" + source_tag + ")")

                    for r in tl_rows:
                        st.markdown(r)

                    # Market snapshot
                    md = ai.get("market_data", {})
                    cd = ai.get("chain_data", {})
                    if md:
                        st.markdown("**Market Data (at scan)**")
                        snap = []
                        if md.get("liquidity_usd"):   snap.append("Liq: $" + f"{md['liquidity_usd']:,.0f}")
                        if md.get("market_cap"):       snap.append("MCap: $" + f"{md['market_cap']:,.0f}")
                        if md.get("volume_24h"):       snap.append("Vol24h: $" + f"{md['volume_24h']:,.0f}")
                        if md.get("change_5m") is not None: snap.append("5m: " + f"{md['change_5m']:+.1f}%")
                        if md.get("change_1h") is not None: snap.append("1h: " + f"{md['change_1h']:+.1f}%")
                        if md.get("buys_h1") is not None:   snap.append("B/S: " + str(md['buys_h1']) + "/" + str(md.get('sells_h1', 0)))
                        if cd.get("top_10_pct"):       snap.append("Top10: " + f"{cd['top_10_pct']:.0f}%")
                        if cd.get("holder_count"):     snap.append("Holders: " + str(cd['holder_count']))
                        for s in snap:
                            st.caption(s)

                with det_r:
                    st.markdown("**Gates (" + str(n_passed) + "/6)**")
                    gate_pct = n_passed / 6
                    gc = "#4ade80" if n_passed >= 5 else "#fbbf24" if n_passed >= 3 else "#f87171"
                    gw = str(int(gate_pct * 100))
                    st.markdown(
                        '<div style="background:#2a2d35;border-radius:4px;height:8px;margin:6px 0 10px 0">'
                        '<div style="background:' + gc + ';border-radius:4px;height:8px;width:' + gw + '%"></div>'
                        '</div>',
                        unsafe_allow_html=True,
                    )
                    for g in all_gates:
                        g_name = g.split(":")[1]
                        passed = g in passed_list
                        icon = "+" if passed else "-"
                        color = "#4ade80" if passed else "#4b5563"
                        st.markdown('<span style="color:' + color + '">' + icon + ' ' + g + '</span>', unsafe_allow_html=True)

                    st.markdown("---")
                    st.markdown("**Links**")
                    if addr:
                        st.markdown(
                            "[DexScreener](https://dexscreener.com/solana/" + addr + ")  \n"
                            "[Solscan](https://solscan.io/token/" + addr + ")  \n"
                            "[RugCheck](https://rugcheck.xyz/tokens/" + addr + ")  \n"
                            "[Birdeye](https://birdeye.so/token/" + addr + "?chain=solana)  \n"
                            "[Jupiter](https://jup.ag/swap/SOL-" + addr + ")  \n"
                            "[Pump.fun](https://pump.fun/" + addr + ")"
                        )
                    st.code(addr, language=None)

                # Strategy Insight
                insight_text = _generate_strategy_insight(dec, stage, n_passed, ai, rej, token_age_h)
                if insight_text:
                    st.markdown(
                        '<div class="insight-box">'
                        '<div class="title">Strategy Insight</div>'
                        '<div class="text">' + insight_text + '</div>'
                        '</div>',
                        unsafe_allow_html=True,
                    )

    else:
        st.info("No trades matching your filters.")


# ══════════════════════════════════════════════════════════════════════════════
# TAB 6 — ANALYTICS
# ══════════════════════════════════════════════════════════════════════════════
with tab_analytics:
    st.markdown('<div class="section-header">Performance Analytics</div>', unsafe_allow_html=True)

    # Funnel chart
    df_funnel = db_query("""
        SELECT funnel_stage, COUNT(*) as count
        FROM trades
        GROUP BY funnel_stage
        ORDER BY count DESC
    """)

    if not df_funnel.empty:
        st.markdown("**Token Funnel** - How many tokens pass each stage")
        fig_funnel = go.Figure(go.Funnel(
            y=df_funnel["funnel_stage"],
            x=df_funnel["count"],
            textinfo="value+percent initial",
            marker=dict(color=["#58a6ff", "#f0883e", "#d29922", "#3fb950", "#f85149", "#8b949e", "#bc8cff", "#39d353"]),
        ))
        fig_funnel.update_layout(
            paper_bgcolor="#0d1117", plot_bgcolor="#0d1117",
            font=dict(color="#e6edf3", family="JetBrains Mono"),
            height=350, margin=dict(l=20, r=20, t=30, b=20),
        )
        st.plotly_chart(fig_funnel, use_container_width=True)

    # Decisions over time
    df_timeline = db_query("""
        SELECT DATE(timestamp) as date, decision, COUNT(*) as count
        FROM trades
        WHERE decision LIKE '%BUY%' OR decision LIKE '%SELL%'
        GROUP BY date, decision
        ORDER BY date
    """)
    if not df_timeline.empty:
        st.markdown("**Trades Over Time**")
        fig_time = px.bar(
            df_timeline, x="date", y="count", color="decision",
            color_discrete_map={
                "BUY": "#3fb950", "BUY (SIMULATED)": "#2ea043",
                "SELL": "#f0883e", "SELL (SIMULATED)": "#d29922",
            },
            barmode="group",
        )
        fig_time.update_layout(
            paper_bgcolor="#0d1117", plot_bgcolor="#161b22",
            font=dict(color="#e6edf3", family="JetBrains Mono"),
            height=300, margin=dict(l=20, r=20, t=30, b=20),
            xaxis=dict(gridcolor="#30363d"), yaxis=dict(gridcolor="#30363d"),
            legend=dict(orientation="h", yanchor="bottom", y=1.02),
        )
        st.plotly_chart(fig_time, use_container_width=True)

    # Score distribution
    df_scores = db_query("""
        SELECT score, decision FROM trades
        WHERE score > 0 AND (decision LIKE '%BUY%' OR decision = 'REJECT' OR decision = 'HOLD' OR decision = 'SKIP')
    """)
    if not df_scores.empty:
        st.markdown("**Score Distribution**")
        fig_score = px.histogram(
            df_scores, x="score", color="decision", nbins=20,
            color_discrete_map={"BUY": "#3fb950", "BUY (SIMULATED)": "#2ea043", "REJECT": "#f85149", "HOLD": "#d29922", "SKIP": "#f85149"},
        )
        fig_score.update_layout(
            paper_bgcolor="#0d1117", plot_bgcolor="#161b22",
            font=dict(color="#e6edf3", family="JetBrains Mono"),
            height=300, margin=dict(l=20, r=20, t=30, b=20),
            xaxis=dict(gridcolor="#30363d", title="Fusion Score"),
            yaxis=dict(gridcolor="#30363d", title="Count"),
            legend=dict(orientation="h", yanchor="bottom", y=1.02),
        )
        st.plotly_chart(fig_score, use_container_width=True)

    # Win/Loss stats
    st.markdown("")
    st.markdown("**Rejection Reasons** - Why tokens were skipped")
    df_rej = db_query("""
        SELECT rejection_reason, COUNT(*) as count
        FROM trades
        WHERE rejection_reason IS NOT NULL AND rejection_reason != ''
        GROUP BY rejection_reason
        ORDER BY count DESC
        LIMIT 15
    """)
    if not df_rej.empty:
        fig_rej = px.bar(
            df_rej, x="count", y="rejection_reason", orientation="h",
            color_discrete_sequence=["#58a6ff"],
        )
        fig_rej.update_layout(
            paper_bgcolor="#0d1117", plot_bgcolor="#161b22",
            font=dict(color="#e6edf3", family="JetBrains Mono", size=11),
            height=max(200, len(df_rej) * 30), margin=dict(l=20, r=20, t=10, b=20),
            xaxis=dict(gridcolor="#30363d"), yaxis=dict(title=""),
        )
        st.plotly_chart(fig_rej, use_container_width=True)


# ══════════════════════════════════════════════════════════════════════════════
# TAB 7 — LOGS
# ══════════════════════════════════════════════════════════════════════════════
with tab_logs:
    st.markdown('<div class="section-header">Bot Logs</div>', unsafe_allow_html=True)

    lc1, lc2, lc3 = st.columns([1, 1, 2])
    with lc1:
        log_level = st.selectbox("Level", ["ALL", "ERROR", "WARNING", "SUCCESS", "INFO"], key="log_level")
    with lc2:
        log_limit = st.selectbox("Entries", [30, 50, 100, 200, 500], index=1, key="log_limit")
    with lc3:
        log_search = st.text_input("Search logs", placeholder="Filter by keyword...", key="log_search")

    where = []
    log_params = []
    if log_level != "ALL":
        where.append("level = ?")
        log_params.append(log_level)
    if log_search:
        where.append("message LIKE ?")
        log_params.append(f"%{log_search}%")

    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    df_log = db_query(
        f"SELECT level, message, timestamp FROM bot_logs {where_sql} ORDER BY timestamp DESC LIMIT ?",
        tuple(log_params + [log_limit]),
    )

    if not df_log.empty:
        # Display as styled log terminal
        st.markdown(
            '<div style="background: #0d1117; border: 1px solid #30363d; border-radius: 8px; '
            'padding: 16px; max-height: 600px; overflow-y: auto; font-family: monospace; font-size: 0.78rem;">',
            unsafe_allow_html=True,
        )
        for _, row in df_log.iterrows():
            lvl = row["level"]
            msg = str(row["message"]).replace("<", "&lt;").replace(">", "&gt;")
            ts  = str(row["timestamp"])[-8:]
            css_map = {"ERROR": "log-error", "WARNING": "log-warning", "SUCCESS": "log-success", "INFO": "log-info"}
            css = css_map.get(lvl, "log-info")
            lvl_badge = {
                "ERROR":   '<span style="background:#3d1418;color:#f85149;padding:1px 6px;border-radius:4px;font-size:0.7rem;">ERR</span>',
                "WARNING": '<span style="background:#2a2000;color:#d29922;padding:1px 6px;border-radius:4px;font-size:0.7rem;">WRN</span>',
                "SUCCESS": '<span style="background:#0d3222;color:#3fb950;padding:1px 6px;border-radius:4px;font-size:0.7rem;">OK</span>',
                "INFO":    '<span style="background:#1c2333;color:#8b949e;padding:1px 6px;border-radius:4px;font-size:0.7rem;">INF</span>',
            }.get(lvl, "")
            st.markdown(
                f'<div style="margin-bottom:3px;">'
                f'<span style="color:#484f58;">{ts}</span> {lvl_badge} '
                f'<span class="{css}">{msg}</span></div>',
                unsafe_allow_html=True,
            )
        st.markdown('</div>', unsafe_allow_html=True)

        # Log stats
        st.markdown("")
        ls1, ls2, ls3, ls4 = st.columns(4)
        total_logs = len(df_log)
        errors   = (df_log["level"] == "ERROR").sum()
        warnings = (df_log["level"] == "WARNING").sum()
        success  = (df_log["level"] == "SUCCESS").sum()
        ls1.metric("Total Shown", total_logs)
        ls2.metric("Errors", int(errors))
        ls3.metric("Warnings", int(warnings))
        ls4.metric("Success", int(success))
    else:
        st.info("No logs matching your filters.")
