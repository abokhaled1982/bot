import json
import streamlit as st
from datetime import datetime
from dashboard.config import WALLET_ADDRESS, POSITION_SIZE_USD
from dashboard.db import db_query, get_live_price, get_reconciled_positions, get_recent_events
from dashboard.components import fmt_usd, fmt_pct, kpi_card, tx_status_html, generate_strategy_insight


ALL_GATES = ["G1:Data", "G2:Safety", "G3:Risk", "G4:PreFilter", "G5:Scoring", "G6:Exec"]

_GATE_ICONS = {
    "G1:Data": "📡", "G2:Safety": "🛡️", "G3:Risk": "⚡",
    "G4:PreFilter": "🔍", "G5:Scoring": "📊", "G6:Exec": "🚀",
}

_AGE_UNIT_MULTIPLIERS = {"min": 1 / 60, "hour": 1, "day": 24}

def token_age_to_hours(value: float, unit: str) -> float:
    """Convert a (value, unit) pair to hours. Unit must be 'min', 'hour', or 'day'."""
    return value * _AGE_UNIT_MULTIPLIERS.get(unit, 1)


def build_search_clause(search: str, mode: str) -> tuple[str, list]:
    """
    Build a SQL WHERE clause + params for the search box.

    Modes:
      Contains   — symbol LIKE '%AC%'  (default, matches SPACE, BACK…)
      Exact      — symbol = 'AC'       (case-insensitive via UPPER)
      Starts with — symbol LIKE 'AC%'  (matches ACID, ACME…)

    Address searches always use contains (addresses are long hex strings).
    """
    if not search or not search.strip():
        return "", []
    s = search.strip()
    if mode == "Exact":
        return "(UPPER(symbol) = UPPER(?) OR token_address LIKE ?)", [s, f"%{s}%"]
    elif mode == "Starts with":
        return "(symbol LIKE ? OR token_address LIKE ?)", [f"{s}%", f"%{s}%"]
    else:  # Contains (default)
        return "(symbol LIKE ? OR token_address LIKE ?)", [f"%{s}%", f"%{s}%"]

def _gates_html(passed_list: list) -> str:
    parts = []
    for g in ALL_GATES:
        passed = g in passed_list
        cls = "pass" if passed else "fail"
        num = g.split(":")[0].replace("G", "")
        parts.append(f'<span class="gate-dot {cls}">{num}</span>')
    return '<span class="gates-bar">' + "".join(parts) + '</span>'


def _hold_duration_str(buy_ts: str, sell_ts: str) -> str:
    try:
        t1 = datetime.strptime(buy_ts[:19], "%Y-%m-%d %H:%M:%S")
        t2 = datetime.strptime(sell_ts[:19], "%Y-%m-%d %H:%M:%S")
        delta = t2 - t1
        hours = delta.total_seconds() / 3600
        if hours < 1:
            return f"{int(delta.total_seconds() / 60)}m"
        elif hours < 24:
            return f"{hours:.1f}h"
        else:
            return f"{hours / 24:.1f}d"
    except Exception:
        return "—"


def render():
    st.markdown('<div class="section-header">📋 Trade History</div>', unsafe_allow_html=True)

    # ── Closed-trade P/L summary ──────────────────────────────────────────────
    df_ev = get_recent_events(limit=500)
    sell_events = df_ev[df_ev["event_type"].str.startswith("SELL_")] if not df_ev.empty else None
    if sell_events is not None and not sell_events.empty:
        total_pnl  = sell_events["pnl_usd"].dropna().sum()
        win_count  = int((sell_events["pnl_usd"].dropna() > 0).sum())
        loss_count = int((sell_events["pnl_usd"].dropna() <= 0).sum())
        total_sold = sell_events["sell_amount_usd"].dropna().sum()
        win_rate   = win_count / (win_count + loss_count) * 100 if (win_count + loss_count) > 0 else 0

        cc1, cc2, cc3, cc4 = st.columns(4)
        pnl_html = f'<span class="{"profit" if total_pnl >= 0 else "loss"}">{fmt_usd(total_pnl)}</span>'
        cc1.markdown(kpi_card("Realized P/L", pnl_html, "closed trades"), unsafe_allow_html=True)
        cc2.markdown(kpi_card("Total Sold", fmt_usd(total_sold), "volume"), unsafe_allow_html=True)
        wr_clr = "profit" if win_rate >= 50 else "loss"
        wr_html = f'<span class="{wr_clr}">{win_rate:.0f}%</span>'
        cc3.markdown(kpi_card("Win Rate", wr_html, f"{win_count}W / {loss_count}L"), unsafe_allow_html=True)
        exit_counts = sell_events["event_type"].value_counts().to_dict()
        exit_parts = []
        exit_icons = {"SELL_TP1": "💚", "SELL_TP2": "💰", "SELL_TP3": "🚀", "SELL_STOP_LOSS": "🛑",
                      "SELL_TRAILING_STOP": "📉", "SELL_TIME_EXIT": "⏰", "SELL_SUCCESS": "✅", "SELL_MANUAL": "🖱️"}
        for k, v in exit_counts.items():
            icon = exit_icons.get(k, "")
            name = k.replace("SELL_", "")
            exit_parts.append(f'{icon}{name}:{v}')
        cc4.markdown(kpi_card("Exit Types", " ".join(exit_parts), f"{len(sell_events)} total"), unsafe_allow_html=True)

    st.markdown("")

    # ── Filters ───────────────────────────────────────────────────────────────
    with st.container():
        fc1, fc2, fc3, fc4, fc5, fc6, fc7, fc8 = st.columns([2.5, 0.8, 1, 1, 1, 1, 1, 0.5])
        with fc1: search      = st.text_input("🔍 Search", placeholder="Symbol or address...", key="hist_search")
        with fc2: search_mode = st.selectbox("Match", ["Contains", "Exact", "Starts with"], key="hist_match")
        with fc3: dec_filter  = st.selectbox("Decision", ["All", "BUY", "SELL", "REJECT", "SKIP"], key="hist_dec")
        with fc4: stg_filter  = st.selectbox("Stage", [
            "All", "DATA_CHECK", "SAFETY_CHECK", "PRE_FILTER", "SCORING",
            "BUY_EXEC", "STOP_LOSS", "TP1", "TP2", "TP3", "TRAILING_STOP", "TIME_EXIT",
        ], key="hist_stage")
        with fc5: gate_filter  = st.selectbox("Gates", [
            "Any", "1+ (Data)", "2+ (Safety)", "3+ (Risk)", "4+ (Pre)", "5+ (Score)", "6 (Bought)"
        ], key="hist_gate")
        with fc6: result_filter = st.selectbox("Result", ["All", "Win", "Loss"], key="hist_result")
        with fc7: period_filter = st.selectbox("Period", ["All time", "Today", "7 days", "30 days"], key="hist_period")
        with fc8: limit = st.selectbox("Rows", [25, 50, 100, 250], index=1, key="hist_limit")

    # ── Token Age filter ──────────────────────────────────────────────────────
    with st.container():
        ac1, ac2, ac3, ac4 = st.columns([1, 0.5, 0.5, 5])
        with ac1: age_mode = st.selectbox("Token Age", ["Any", "≤ Max", "≥ Min"], key="hist_age_mode")
        with ac2: age_value = st.number_input("Value", min_value=0, value=0, step=1, key="hist_age_val")
        with ac3: age_unit = st.selectbox("Unit", ["min", "hour", "day"], key="hist_age_unit")

    # Build SQL
    where, params = [], []
    if search:
        clause, search_params = build_search_clause(search, search_mode)
        if clause:
            where.append(clause)
            params.extend(search_params)
    if dec_filter != "All":
        where.append("decision LIKE ?")
        params.append(f"%{dec_filter}%")
    if stg_filter != "All":
        where.append("funnel_stage = ?")
        params.append(stg_filter)
    gate_min = {"1+ (Data)": 1, "2+ (Safety)": 2, "3+ (Risk)": 3,
                "4+ (Pre)": 4, "5+ (Score)": 5, "6 (Bought)": 6}
    if gate_filter in gate_min:
        n = gate_min[gate_filter]
        if n == 1: where.append("gates_passed IS NOT NULL AND gates_passed != ''")
        else:      where.append(f"(LENGTH(gates_passed)-LENGTH(REPLACE(gates_passed,',','')))>={n-1}")
    if period_filter == "Today":
        where.append("DATE(timestamp) = DATE('now')")
    elif period_filter == "7 days":
        where.append("timestamp >= DATETIME('now', '-7 days')")
    elif period_filter == "30 days":
        where.append("timestamp >= DATETIME('now', '-30 days')")

    # Token age filter (token_age_hours column)
    if age_mode != "Any" and age_value > 0:
        age_hours = token_age_to_hours(age_value, age_unit)
        if age_mode == "≤ Max":
            where.append("token_age_hours IS NOT NULL AND token_age_hours <= ?")
            params.append(age_hours)
        elif age_mode == "≥ Min":
            where.append("token_age_hours IS NOT NULL AND token_age_hours >= ?")
            params.append(age_hours)

    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    df = db_query(
        f"""SELECT id, symbol, token_address, entry_price, position_size,
                   buy_amount_usd, sell_amount_usd,
                   score, decision, rejection_reason, ai_reasoning,
                   funnel_stage, gates_passed, timestamp, tx_signature, tx_status
            FROM trades {where_sql} ORDER BY timestamp DESC LIMIT ?""",
        tuple(params + [limit]),
    )

    if df.empty:
        st.info("No trades match the current filters.")
        return

    # Pre-fetch all buys & sells for cross-reference
    buy_lookup = {}
    for _, b in db_query("SELECT token_address, entry_price, buy_amount_usd, timestamp FROM trades WHERE decision LIKE '%BUY%'").iterrows():
        buy_lookup[str(b["token_address"])] = {
            "buy_price":  float(b["entry_price"] or 0),
            "buy_amount": float(b.get("buy_amount_usd") or 0),
            "buy_time":   str(b["timestamp"] or ""),
        }

    sell_lookup = {}
    for _, s in db_query("SELECT token_address, entry_price, sell_amount_usd, rejection_reason, timestamp FROM trades WHERE decision LIKE '%SELL%'").iterrows():
        sell_lookup.setdefault(str(s["token_address"]), []).append(s)

    positions = get_reconciled_positions(WALLET_ADDRESS)
    st.caption(f"Showing {len(df)} records")

    for _, row in df.iterrows():
        addr    = str(row["token_address"] or "")
        sym     = str(row["symbol"]        or addr[:8])
        dec     = str(row["decision"]      or "")
        ep      = float(row["entry_price"]    or 0)
        buy_usd = float(row.get("buy_amount_usd",  0) or 0)
        sel_usd = float(row.get("sell_amount_usd", 0) or 0)
        score   = float(row["score"]         or 0)
        rej     = str(row["rejection_reason"] or "")
        ts      = str(row["timestamp"]        or "")[:19]
        stage   = str(row["funnel_stage"]     or "")
        gates   = str(row["gates_passed"]     or "")
        ai_raw  = str(row["ai_reasoning"]     or "")
        tx_sig  = str(row["tx_signature"]     or "") if row.get("tx_signature") else ""
        tx_st   = str(row["tx_status"]        or "") if row.get("tx_status")    else ""

        ai = {}
        if ai_raw:
            try: ai = json.loads(ai_raw)
            except Exception: pass

        passed_list = [g.strip() for g in gates.split(",") if g.strip()] if gates else []

        # P/L calculations
        buy_ref   = buy_lookup.get(addr, {})
        buy_p     = buy_ref.get("buy_price",  0)
        buy_a     = buy_ref.get("buy_amount", 0)
        buy_t     = buy_ref.get("buy_time",   "")
        sell_list = sell_lookup.get(addr, [])

        sell_pct   = ((ep - buy_p) / buy_p * 100) if "SELL" in dec and buy_p > 0 and ep > 0 else 0
        sell_plusd  = (sel_usd - buy_a) if sel_usd > 0 and buy_a > 0 else (
            (POSITION_SIZE_USD * sell_pct / 100) if sell_pct != 0 else 0
        )
        pos_open = "BUY" in dec and addr in positions
        live_pct = 0.0
        if pos_open:
            cp = get_live_price(addr)
            live_pct = ((cp - ep) / ep * 100) if ep > 0 and cp > 0 else 0

        # Result filter — only applies to BUY/SELL rows, skip everything else
        if result_filter == "Win":
            if "SELL" in dec and sell_pct <= 0: continue
            elif "BUY" in dec and live_pct <= 0: continue
            elif "SELL" not in dec and "BUY" not in dec: continue
        if result_filter == "Loss":
            if "SELL" in dec and sell_pct >= 0: continue
            elif "BUY" in dec and live_pct >= 0: continue
            elif "SELL" not in dec and "BUY" not in dec: continue

        # ── Expander label (rich) ────────────────────────────────────────────
        tx_icon = {"confirmed": "✅", "unconfirmed": "⚠️", "error": "❌"}.get(tx_st, "")

        if "BUY" in dec:
            icon = "🟢" if pos_open else "⬛"
            if pos_open:
                pl_tag = f'{live_pct:+.1f}% live'
            elif sell_list:
                last_sell = sell_list[-1]
                lsp = float(last_sell["entry_price"] or 0)
                lpct = ((lsp - ep) / ep * 100) if ep > 0 and lsp > 0 else 0
                pl_tag = f'{lpct:+.1f}%'
            else:
                pl_tag = "closed"
            exp_label = f"{icon} **{sym}** · BUY @ ${ep:.8f} · {pl_tag} {tx_icon} · {ts[:16]}"
        elif "SELL" in dec:
            sg = "+" if sell_pct >= 0 else ""
            exp_label = (
                f"🔴 **{sym}** · SELL @ ${ep:.8f} · "
                f"{sg}{sell_pct:.1f}% · {fmt_usd(sel_usd)} {tx_icon} · {ts[:16]}"
            )
        elif dec in ("REJECT", "SKIP"):
            exp_label = f"🔵 **{sym}** · {dec} · Score {score:.0f} · {stage} · {ts[:16]}"
        else:
            exp_label = f"⚫ **{sym}** · {dec} {tx_icon} · {ts[:16]}"

        with st.expander(exp_label, expanded=False):
            # ── Top: P/L hero + gates visual ─────────────────────────────────
            hero_col, gates_col = st.columns([3, 1])
            with hero_col:
                if "SELL" in dec and buy_p > 0:
                    clr = "profit" if sell_pct >= 0 else "loss"
                    sg  = "+" if sell_pct >= 0 else ""
                    hold_str = _hold_duration_str(buy_t, ts)
                    st.markdown(
                        f'<div style="display:flex;align-items:baseline;gap:16px;margin-bottom:8px">'
                        f'<span class="{clr}" style="font-size:1.6rem;font-weight:800">{sg}{sell_pct:.2f}%</span>'
                        f'<span class="{clr}" style="font-size:1.2rem">{sg}{fmt_usd(abs(sell_plusd))}</span>'
                        f'<span style="color:#e0a846;font-size:0.9rem">held {hold_str}</span>'
                        f'</div>',
                        unsafe_allow_html=True,
                    )
                elif "BUY" in dec:
                    if pos_open:
                        cp_l = get_live_price(addr)
                        l_pct = ((cp_l - ep) / ep * 100) if ep > 0 and cp_l > 0 else 0
                        l_usd = (buy_usd / ep * cp_l - buy_usd) if ep > 0 and cp_l > 0 and buy_usd > 0 else 0
                        clr = "profit" if l_pct >= 0 else "loss"
                        sg = "+" if l_pct >= 0 else ""
                        st.markdown(
                            f'<div style="display:flex;align-items:baseline;gap:16px;margin-bottom:8px">'
                            f'<span style="background:rgba(0,230,167,0.15);color:#00e6a7;padding:3px 12px;border-radius:6px;font-weight:700;font-size:0.88rem">OPEN</span>'
                            f'<span class="{clr}" style="font-size:1.4rem;font-weight:800">{sg}{l_pct:.1f}%</span>'
                            f'<span class="{clr}" style="font-size:1.05rem">{sg}{fmt_usd(abs(l_usd))}</span>'
                            f'</div>',
                            unsafe_allow_html=True,
                        )
                    elif sell_list:
                        total_sold_usd = sum(float(s.get("sell_amount_usd") or 0) for s in sell_list)
                        net = total_sold_usd - buy_usd if buy_usd > 0 else 0
                        net_pct = (net / buy_usd * 100) if buy_usd > 0 else 0
                        clr = "profit" if net >= 0 else "loss"
                        sg = "+" if net >= 0 else ""
                        st.markdown(
                            f'<div style="display:flex;align-items:baseline;gap:16px;margin-bottom:8px">'
                            f'<span style="background:rgba(100,116,139,0.15);color:#c4b5fd;padding:3px 12px;border-radius:6px;font-weight:700;font-size:0.88rem">CLOSED</span>'
                            f'<span class="{clr}" style="font-size:1.4rem;font-weight:800">{sg}{net_pct:.1f}%</span>'
                            f'<span class="{clr}" style="font-size:1.05rem">{sg}{fmt_usd(abs(net))}</span>'
                            f'</div>',
                            unsafe_allow_html=True,
                        )
                    else:
                        st.markdown(
                            '<span style="background:rgba(100,116,139,0.15);color:#c4b5fd;padding:3px 12px;border-radius:6px;font-weight:700;font-size:0.88rem">CLOSED</span>',
                            unsafe_allow_html=True,
                        )
                else:
                    st.markdown(
                        f'<div style="display:flex;align-items:baseline;gap:12px;margin-bottom:8px">'
                        f'<span style="color:#ff5c5c;font-weight:700;font-size:1.1rem">⛔ {dec}</span>'
                        f'<span style="color:#e0a846;font-size:0.92rem">{stage}</span>'
                        f'<span style="color:#7cb4ff;font-size:0.88rem">Score: {score:.0f}</span>'
                        f'</div>',
                        unsafe_allow_html=True,
                    )

            with gates_col:
                st.markdown(f'<div style="text-align:right;padding-top:4px">{_gates_html(passed_list)}</div>', unsafe_allow_html=True)
                st.markdown(f'<div style="text-align:right;color:#e0a846;font-size:0.82rem">{len(passed_list)}/6 gates</div>', unsafe_allow_html=True)

            # ── Detail grid: ALL trade data ──────────────────────────────────
            st.markdown(
                '<div class="detail-grid">'
                + _detail_item("📅 Time", ts[:16])
                + _detail_item("🏷️ Decision", dec)
                + _detail_item("📊 Score", f"{score:.1f}")
                + _detail_item("🔧 Stage", stage)
                + _detail_item("💲 Entry Price", f"${ep:.8f}" if ep > 0 else "—")
                + _detail_item("💵 Buy Amount", fmt_usd(buy_usd) if buy_usd > 0 else (fmt_usd(buy_a) if buy_a > 0 else "—"))
                + _detail_item("💸 Sell Amount", fmt_usd(sel_usd) if sel_usd > 0 else "—")
                + _detail_item("🔗 TX Status", tx_st.upper() if tx_st else "—")
                + '</div>',
                unsafe_allow_html=True,
            )

            # ── Buy ↔ Sell cross-reference ───────────────────────────────────
            if "SELL" in dec and buy_p > 0:
                st.markdown(
                    '<div class="detail-grid" style="border-top:1px solid #151b27;padding-top:12px">'
                    + _detail_item("🛒 Buy Price", f"${buy_p:.8f}")
                    + _detail_item("🛒 Buy Time", buy_t[:16] if buy_t else "—")
                    + _detail_item("🛒 Buy Amount", fmt_usd(buy_a) if buy_a > 0 else "—")
                    + _detail_item("📤 Sell Price", f"${ep:.8f}")
                    + _detail_item("📤 Sell Time", ts[:16])
                    + _detail_item("📤 Sell Amount", fmt_usd(sel_usd) if sel_usd > 0 else "—")
                    + _detail_item("⏱️ Hold Duration", _hold_duration_str(buy_t, ts))
                    + _detail_item("📈 P/L", f'<span class="{"profit" if sell_pct >= 0 else "loss"}">{sell_pct:+.1f}%</span>')
                    + '</div>',
                    unsafe_allow_html=True,
                )

            elif "BUY" in dec and sell_list:
                st.markdown("---")
                st.markdown(
                    '<span style="color:#7cb4ff;font-size:0.88rem;font-weight:700;text-transform:uppercase;letter-spacing:0.5px">'
                    '📤 Sell Events</span>',
                    unsafe_allow_html=True,
                )
                for sr in sell_list:
                    sp   = float(sr["entry_price"] or 0)
                    s_u  = float(sr.get("sell_amount_usd") or 0)
                    spct = ((sp - ep) / ep * 100) if ep > 0 and sp > 0 else 0
                    clr  = "profit" if spct >= 0 else "loss"
                    s_ts = str(sr["timestamp"] or "")[:16]
                    reason = str(sr.get("rejection_reason") or "")[:60]
                    hold = _hold_duration_str(ts, str(sr["timestamp"] or ""))
                    st.markdown(
                        f'<div style="display:flex;gap:16px;align-items:center;padding:6px 0;border-bottom:1px solid #0f1420">'
                        f'<span style="color:#5eead4;font-size:0.85rem;font-family:JetBrains Mono,monospace">{s_ts}</span>'
                        f'<span style="color:#ffffff;font-size:0.92rem;font-family:JetBrains Mono,monospace">${sp:.8f}</span>'
                        f'<span class="{clr}" style="font-weight:700;font-size:0.95rem">{spct:+.1f}%</span>'
                        f'<span style="color:#ffffff;font-size:0.92rem">{fmt_usd(s_u)}</span>'
                        f'<span style="color:#e0a846;font-size:0.85rem">held {hold}</span>'
                        f'<span style="color:#c4b5fd;font-size:0.85rem">{reason}</span>'
                        f'</div>',
                        unsafe_allow_html=True,
                    )

            # ── Rejection / acceptance reason ────────────────────────────────
            # For BUY trades: accept_detail is in ai_reasoning JSON (not rejection_reason)
            display_reason = rej
            if not display_reason and "BUY" in dec:
                display_reason = ai.get("accept_detail", "")
            if display_reason:
                is_accept = "BUY" in dec or "SELL" in dec or display_reason.startswith("[ACCEPT]")
                if is_accept:
                    border_clr, label_clr, label_txt = "#00e6a7", "#00e6a7", "Trade Signal"
                else:
                    border_clr, label_clr, label_txt = "#ff5c5c", "#ff5c5c", "Rejection Reason"
                st.markdown(
                    f'<div style="background:#0d0f16;border:1px solid #1e2536;border-left:3px solid {border_clr};'
                    f'border-radius:8px;padding:10px 14px;margin-top:8px">'
                    f'<span style="color:{label_clr};font-size:0.85rem;font-weight:700;text-transform:uppercase">{label_txt}</span>'
                    f'<div style="color:#ffffff;font-size:0.92rem;margin-top:5px;font-family:JetBrains Mono,monospace">{display_reason}</div>'
                    f'</div>',
                    unsafe_allow_html=True,
                )

            # ── Token info from AI reasoning ─────────────────────────────────
            md = ai.get("market_data", {})
            cd = ai.get("chain_data",  {})
            if md or cd:
                st.markdown("---")
                st.markdown(
                    '<span style="color:#7cb4ff;font-size:0.85rem;font-weight:700;text-transform:uppercase;letter-spacing:0.5px">'
                    '📊 Token Data at Scan</span>',
                    unsafe_allow_html=True,
                )
                parts = []
                if md:
                    parts += [
                        _detail_item("💧 Liquidity", f"${md.get('liquidity_usd', 0):,.0f}"),
                        _detail_item("📈 MCap", f"${md.get('market_cap', 0):,.0f}"),
                        _detail_item("5m Change", f"{md.get('change_5m', 0):+.1f}%"),
                        _detail_item("1h Change", f"{md.get('change_1h', 0):+.1f}%"),
                        _detail_item("🛒 Buys 1h", str(md.get('buys_h1', 0))),
                        _detail_item("📤 Sells 1h", str(md.get('sells_h1', 0))),
                    ]
                if cd:
                    parts += [
                        _detail_item("🏦 Top 10%", f"{cd.get('top_10_pct', '?')}%"),
                        _detail_item("👥 Holders", str(cd.get('holder_count', '?'))),
                    ]
                age_h   = ai.get("token_age_hours", -1)
                src_tag = ai.get("source", "")
                is_mig  = ai.get("is_migration", False)
                if age_h >= 0:
                    age_str = f"{age_h*60:.0f}m" if age_h < 1 else (f"{age_h:.1f}h" if age_h < 24 else f"{age_h/24:.1f}d")
                    parts.append(_detail_item("🕐 Token Age", age_str))
                    parts.append(_detail_item("📡 Source", "MIGRATION" if is_mig else src_tag))

                st.markdown('<div class="detail-grid">' + "".join(parts) + '</div>', unsafe_allow_html=True)

            # Signals & risk flags
            sigs = ai.get("key_signals", [])
            risk_flags = ai.get("risk_flags", [])
            if sigs or risk_flags:
                sig_col, risk_col = st.columns(2)
                with sig_col:
                    if sigs:
                        st.markdown(
                            '<span style="color:#e0a846;font-size:0.82rem;font-weight:700;text-transform:uppercase">Signals</span>',
                            unsafe_allow_html=True,
                        )
                        for s in sigs:
                            st.markdown(
                                f'<span style="color:#7cb4ff;font-size:0.88rem">• {s}</span>',
                                unsafe_allow_html=True,
                            )
                with risk_col:
                    if risk_flags:
                        clean = risk_flags == ["No_Risk_Flags"]
                        clr = "#00e6a7" if clean else "#ff5c5c"
                        st.markdown(
                            f'<span style="color:#e0a846;font-size:0.82rem;font-weight:700;text-transform:uppercase">Risk Flags</span>',
                            unsafe_allow_html=True,
                        )
                        for f in risk_flags:
                            st.markdown(
                                f'<span style="color:{clr};font-size:0.88rem;font-weight:600">• {f}</span>',
                                unsafe_allow_html=True,
                            )

            # ── Links & TX ───────────────────────────────────────────────────
            st.markdown("---")
            link_col, tx_col, addr_col = st.columns([2, 2, 2])
            with link_col:
                st.markdown(
                    f'<div style="display:flex;gap:12px;flex-wrap:wrap">'
                    f'<a href="https://dexscreener.com/solana/{addr}" target="_blank" style="color:#3b8bff;font-size:0.88rem;text-decoration:none">📊 DexScreener</a>'
                    f'<a href="https://solscan.io/token/{addr}" target="_blank" style="color:#3b8bff;font-size:0.88rem;text-decoration:none">🔗 Solscan</a>'
                    f'<a href="https://rugcheck.xyz/tokens/{addr}" target="_blank" style="color:#3b8bff;font-size:0.88rem;text-decoration:none">🛡️ RugCheck</a>'
                    f'<a href="https://jup.ag/swap/SOL-{addr}" target="_blank" style="color:#3b8bff;font-size:0.88rem;text-decoration:none">🪐 Jupiter</a>'
                    f'</div>',
                    unsafe_allow_html=True,
                )
            with tx_col:
                if tx_st:
                    st.markdown(tx_status_html(tx_st), unsafe_allow_html=True)
                if tx_sig:
                    st.markdown(f'<a href="https://solscan.io/tx/{tx_sig}" target="_blank" style="color:#3b8bff;font-size:0.88rem">🔗 View TX on Solscan</a>', unsafe_allow_html=True)
            with addr_col:
                st.code(addr, language=None)

            # ── Strategy insight ─────────────────────────────────────────────
            insight = generate_strategy_insight(dec, stage, ai, rej)
            if insight:
                st.markdown(
                    f'<div class="insight-box"><div class="title">💡 Strategy Insight</div>'
                    f'<div class="text">{insight}</div></div>',
                    unsafe_allow_html=True,
                )


def _detail_item(label: str, value: str) -> str:
    return (
        f'<div class="detail-item">'
        f'<div class="detail-label">{label}</div>'
        f'<div class="detail-value">{value}</div>'
        f'</div>'
    )
