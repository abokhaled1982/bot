"""
Reusable UI components shared across all dashboard tabs.
"""


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
    return (
        f'<div class="kpi-card">'
        f'<div class="label">{label}</div>'
        f'<div class="value">{value}</div>'
        f'<div class="sub">{sub}</div>'
        f'</div>'
    )


def tx_badge_html(tx_status: str) -> str:
    """Returns colored TX status badge string for expander labels."""
    if tx_status == "confirmed":   return " ✅"
    if tx_status == "unconfirmed": return " ⚠️ UNCONFIRMED"
    if tx_status == "error":       return " ❌ TX ERROR"
    return ""


def tx_status_html(tx_status: str) -> str:
    """Returns inline HTML for the TX status color label in the detail panel."""
    color_map = {
        "confirmed":   "#22c55e",
        "unconfirmed": "#f59e0b",
        "error":       "#ef4444",
    }
    color = color_map.get(tx_status, "#e0a846")
    return (
        f'TX Status: <span style="color:{color};font-weight:700">'
        f'{tx_status.upper()}</span>'
    )


def generate_strategy_insight(dec: str, stage: str, ai: dict, rej: str) -> str:
    """Human-readable explanation of why a token was bought or rejected."""
    hints = []
    md     = ai.get("market_data", {}) if ai else {}
    cd     = ai.get("chain_data",  {}) if ai else {}
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

    elif stage == "SAFETY_CHECK":
        hints.append("RugCheck hat diesen Token als unsicher eingestuft.")
        hints.append("Moegliche Gruende: Mint Authority nicht revoked, Freeze Authority aktiv, bekannter Scam.")

    elif stage == "PRE_FILTER":
        if "Liq zu niedrig" in rej or "Migration Liq" in rej:
            hints.append("Liquiditaet war unter dem Minimum. Niedrige Liq = hohes Rug-Pull Risiko.")
        elif "fällt" in rej or "dumpt" in rej:
            hints.append("Token war im Abwaertstrend. Der Bot kauft nicht in fallende Messer.")
        elif "Spike zu niedrig" in rej:
            hints.append("Kein ausreichender Volume-Spike.")
        elif "Verkaufsdruck" in rej or "Kaufdruck" in rej:
            hints.append("Mehr Sells als Buys — der Markt verkauft diesen Token aktiv.")
        elif "zu neu" in rej.lower():
            hints.append("Token war < 1h alt. Sehr neue Tokens haben ein hohes Rug-Risiko.")
        elif "Critical Flag" in rej:
            flag = rej.split("Critical Flag: ")[-1].split("|")[0].strip() if "Critical Flag:" in rej else ""
            hints.append(f"Risk Flag '{flag}' hat den Token geblockt.")
            if "Heavy_Selling"    in rej: hints.append("Mehr als doppelt so viele Sells wie Buys.")
            if "Wash_Trading"     in rej: hints.append("Hohe Volume bei sehr wenigen Transaktionen.")
            if "Liquidity_Drain"  in rej: hints.append("Preis crasht und Volumen >> Liquiditaet.")

    elif stage == "SCORING":
        if "Override" in rej:
            override = rej.split("Override: ")[1].split("|")[0].strip() if "Override:" in rej else ""
            hints.append(f"Override hat eingegriffen: {override}")
        else:
            hints.append("Fusion Score hat nicht gereicht (min 65 fuer BUY).")
            if md.get("change_1h", 0) < 5:
                hints.append("Schwaches 1h-Momentum reduziert den Hype-Score stark.")
            if cd.get("top_10_pct", 0) > 50:
                hints.append("Hohe Wallet-Konzentration drueckt den Score runter.")

    elif "STOP_LOSS"  in stage: hints.append("Position per Stop-Loss geschlossen. Verlust begrenzt.")
    elif "TP3"        in stage: hints.append("+200% — exzellenter Trade. Strategie perfekt.")
    elif "TP2"        in stage: hints.append("Take-Profit 2 erreicht. Gewinne teilweise realisiert.")
    elif "TP1"        in stage: hints.append("50% der Position bei +50% verkauft.")
    elif "TRAILING"   in stage: hints.append("Trailing Stop hat ausgeloest. Gewinne gesichert.")
    elif "TIME_EXIT"  in stage: hints.append("Position nach 24h geschlossen (kein ausreichender Gewinn).")

    return " ".join(hints)
